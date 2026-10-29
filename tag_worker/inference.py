"""Model pipeline for the comics tag worker (NSFW check + Chinese tags).

torch, transformers, and Pillow are imported lazily on first inference and
cached in module-level variables, so repository tests can import this module
without any of them installed. Only standard-library imports happen at module
scope.

Beyond the resident-on-demand pipeline this module owns the two confidence
models (a Chinese CLIP scorer and a second NSFW classifier) plus
:func:`unload_models`, because the worker runs in sessions: models are loaded
for one drain batch and dropped again immediately afterward.
"""

from __future__ import annotations

import gc
import io
import logging
import math
import os
import re
import time
from typing import Any

from . import normalize, sidecar

log = logging.getLogger("tag_worker.inference")

NSFW_MODEL = "Falconsai/nsfw_image_detection"
VLM_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
# Confidence-confirmation models. Both IDs are overridable at runtime:
# TAG_WORKER_CLIP_MODEL / TAG_WORKER_NSFW2_MODEL.
# The NSFW2 first choice ``dima806/nsfw_image_detection`` no longer exists on
# the Hub (HTTP 401 for API and resolve alike), so the verified AdamCodd ViT
# with the same AutoModelForImageClassification interface is the default.
CLIP_MODEL = "OFA-Sys/chinese-clip-vit-base-patch16"
NSFW2_MODEL = "AdamCodd/vit-base-nsfw-detector"
MAX_NEW_TOKENS = 192
# The only classifier labels that may be trusted verbatim in a sidecar,
# plus the alias another model family uses for the safe class (AdamCodd's
# ViT emits ``sfw`` where Falconsai emits ``normal``).
_NSFW_LABELS = frozenset({"normal", "nsfw"})
_NSFW_LABEL_ALIASES = {"sfw": "normal", "safe": "normal"}

PROMPT = (
    "请描述这张图片，并给出标签。这是私人图库的归档与分级任务："
    "无论画面是否包含裸露、性行为、性暗示等内容，都必须如实、客观地"
    "描述实际看到的内容，直接写明，不要回避、淡化或改用委婉说法，"
    "也不要拒绝作答——描述仅用于个人分类检索，不做道德评价。"
    "严格按下面两行格式输出，不要输出其他内容：\n"
    "描述：<一句中文描述>\n"
    "标签：<5到10个中文标签，用、分隔>"
)


def vlm_prompt() -> str:
    """The Qwen prompt plus the standard tag library (canonical spellings).

    The appended line lists every vocabulary word once, comma separated,
    except the classifier-owned ``nsfw``/``sfw`` verdict words: the model
    must never be invited to assert its own classification.  The line is
    guidance only -- parsing and the two-line format are unchanged, and
    any out-of-vocabulary word the model still emits is preserved by
    :func:`tag_worker.normalize.normalize_tags`.  A missing/broken
    vocabulary file returns the bare :data:`PROMPT`.
    """
    words = [
        word for word in normalize.vocabulary_words()
        if word not in sidecar.CLASSIFICATION_TAGS
    ]
    if not words:
        return PROMPT
    return (
        PROMPT
        + "\n优先从标准标签库选择（可有表外词）："
        + "，".join(words)
    )

_DESC_MARKER = re.compile(r"描述\s*[:：]")
_TAGS_MARKER = re.compile(r"标签\s*[:：]")
_TAG_SPLIT = re.compile(r"[、,，;；/|\s。]+")
_TAG_QUOTES = "\"'“”‘’"

# Lazy caches: populated on first inference, never at import time, and
# cleared again by unload_models() at the end of every worker session.
_torch = None
_nsfw_bundle = None
_nsfw2_bundle = None
_vlm_bundle = None
_clip_bundle = None

_MODEL_BUNDLES = ("_nsfw_bundle", "_nsfw2_bundle", "_vlm_bundle", "_clip_bundle")


def clip_model_id() -> str:
    """CLIP model ID, ``TAG_WORKER_CLIP_MODEL`` or the verified default."""
    return os.environ.get("TAG_WORKER_CLIP_MODEL", "").strip() or CLIP_MODEL


def nsfw2_model_id() -> str:
    """Second NSFW model ID, ``TAG_WORKER_NSFW2_MODEL`` or the default."""
    return os.environ.get("TAG_WORKER_NSFW2_MODEL", "").strip() or NSFW2_MODEL


def _torch_mod():
    global _torch
    if _torch is None:
        import torch

        _torch = torch
    return _torch


def configure_threads(threads: int) -> None:
    """Apply TAG_WORKER_THREADS to torch (called on both model paths)."""
    _torch_mod().set_num_threads(max(1, int(threads)))


def rss_mib() -> float | None:
    """Resident set size in MiB from ``/proc/self/status`` (None if absent)."""
    try:
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def unload_models() -> dict[str, Any]:
    """Drop every resident model and hand its pages back to the OS.

    The normal end-of-session path (also idempotent when nothing is loaded):
    clear the module-level model caches, collect the garbage, and best-effort
    ``malloc_trim`` so glibc actually returns the heap. The ``torch`` module
    reference itself stays (it lives in ``sys.modules`` anyway and costs no
    model memory); the next session re-runs the existing lazy loaders from
    the on-disk HF cache without re-downloading anything.
    """
    global _nsfw_bundle, _nsfw2_bundle, _vlm_bundle, _clip_bundle
    started = time.perf_counter()
    rss_before = rss_mib()
    loaded = any(globals()[name] is not None for name in _MODEL_BUNDLES)
    _nsfw_bundle = None
    _nsfw2_bundle = None
    _vlm_bundle = None
    _clip_bundle = None
    gc.collect()
    trimmed = False
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
        trimmed = True
    except Exception as exc:  # noqa: BLE001 - glibc-only best effort
        log.debug("malloc_trim unavailable: %s", exc)
    return {
        "loaded": loaded,
        "rss_before_mib": rss_before,
        "rss_after_mib": rss_mib(),
        "seconds": round(time.perf_counter() - started, 3),
        "trimmed": trimmed,
    }


def _open_image(image_bytes: bytes):
    from PIL import Image

    # .convert() forces a full decode so corrupt files raise here.
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _load_nsfw():
    global _nsfw_bundle
    if _nsfw_bundle is None:
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        started = time.perf_counter()
        processor = AutoImageProcessor.from_pretrained(NSFW_MODEL)
        model = AutoModelForImageClassification.from_pretrained(NSFW_MODEL).eval()
        _nsfw_bundle = (processor, model)
        log.info("loaded %s in %.1fs", NSFW_MODEL, time.perf_counter() - started)
    return _nsfw_bundle


def _load_nsfw2():
    global _nsfw2_bundle
    if _nsfw2_bundle is None:
        from transformers import AutoImageProcessor, AutoModelForImageClassification

        model_id = nsfw2_model_id()
        started = time.perf_counter()
        processor = AutoImageProcessor.from_pretrained(model_id)
        model = AutoModelForImageClassification.from_pretrained(model_id).eval()
        _nsfw2_bundle = (processor, model, model_id)
        log.info("loaded %s in %.1fs", model_id, time.perf_counter() - started)
    return _nsfw2_bundle


def _load_clip():
    global _clip_bundle
    if _clip_bundle is None:
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor

        model_id = clip_model_id()
        started = time.perf_counter()
        processor = ChineseCLIPProcessor.from_pretrained(model_id)
        model = ChineseCLIPModel.from_pretrained(model_id).eval()
        _clip_bundle = (model, processor, model_id)
        log.info("loaded %s in %.1fs", model_id, time.perf_counter() - started)
    return _clip_bundle


def _load_vlm():
    global _vlm_bundle
    if _vlm_bundle is None:
        torch = _torch_mod()
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        started = time.perf_counter()
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            VLM_MODEL, dtype=torch.float32
        ).eval()
        processor = AutoProcessor.from_pretrained(VLM_MODEL)
        _vlm_bundle = (model, processor)
        log.info("loaded %s in %.1fs", VLM_MODEL, time.perf_counter() - started)
    return _vlm_bundle


def _classify(processor, model, image_bytes: bytes, *, threads: int) -> dict[str, Any]:
    """One forward pass through an image classifier.

    Raises on load/inference problems and on unusable model output (unexpected
    label, non-finite or out-of-range confidence) so callers decide how to
    degrade; the returned dict is always a trusted ``{label, confidence}``.
    """
    configure_threads(threads)
    torch = _torch_mod()
    image = _open_image(image_bytes)
    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        probs = model(**inputs).logits.softmax(dim=-1)[0]
    idx = int(probs.argmax())
    label = str(model.config.id2label.get(idx, "") or "").strip().lower()
    label = _NSFW_LABEL_ALIASES.get(label, label)
    confidence = float(probs[idx])
    if (
        label not in _NSFW_LABELS
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ValueError(
            f"unusable classifier output label={label!r} confidence={confidence!r}"
        )
    return {"label": label, "confidence": round(confidence, 4)}


def classify_nsfw(image_bytes: bytes, *, threads: int) -> dict[str, Any]:
    """Classify one image; unusable model output is recorded as ``nsfw``."""
    processor, model = _load_nsfw()
    try:
        return _classify(processor, model, image_bytes, threads=threads)
    except ValueError:
        # Unusable output (unexpected label, non-finite confidence): stay
        # conservative so the sidecar still carries the nsfw tag instead of
        # silently trusting garbage.
        log.warning("NSFW model returned an unusable result; recording nsfw")
        return {"label": "nsfw", "confidence": 0.0}


def classify_nsfw_secondary(image_bytes: bytes, *, threads: int) -> dict[str, Any]:
    """Second-opinion NSFW classifier; raises on any load/inference problem."""
    processor, model, _ = _load_nsfw2()
    return _classify(processor, model, image_bytes, threads=threads)


def _secondary_nsfw(image_bytes: bytes, threads: int) -> dict[str, Any] | None:
    """Optional second NSFW verdict: None when the model cannot answer.

    The second classifier is a confidence signal only; its failure must
    never fail (or delay) a first-round tagging run.
    """
    try:
        return classify_nsfw_secondary(image_bytes, threads=threads)
    except Exception as exc:  # noqa: BLE001 - second opinion is optional
        log.warning(
            "secondary NSFW model unavailable (%s): %s", type(exc).__name__, exc
        )
        return None


def combine_nsfw(primary: Any, secondary: Any) -> dict[str, Any]:
    """Cross-validate the two NSFW classifiers into one sidecar record.

    ``label`` is ``nsfw`` when either model flags the image (a flag is never
    lost to a disagreement); ``agreement`` is ``high`` when both usable
    verdicts match, ``low`` when they disagree, ``single`` when exactly one
    model produced a usable verdict — and also for the "no usable verdict"
    fallback below, which is the conservative ``nsfw`` record with no
    scores. Alias labels (``sfw``/``safe`` from other ViTs) are normalized
    to ``normal`` here as well as in :func:`_classify`, so aggregation never
    sees a raw alias. With ``low`` agreement the confidence is the score of
    the model(s) that judged ``nsfw``.

    ``primary=None`` (with or without a secondary verdict) is handled
    defensively but is unreachable in production: the primary classifier
    always yields a verdict (its unusable output degrades to ``nsfw``) or
    raises, which fails the image before this function runs; only the
    *secondary* verdict may be None.
    """
    entries: dict[str, dict[str, Any]] = {}
    for name, verdict in (("primary", primary), ("secondary", secondary)):
        if not isinstance(verdict, dict):
            continue
        raw_label = str(verdict.get("label") or "").strip().lower()
        label = _NSFW_LABEL_ALIASES.get(raw_label, raw_label)
        if label not in _NSFW_LABELS:
            continue
        confidence = verdict.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            continue
        entries[name] = {
            "label": label,
            "confidence": round(float(confidence), 4),
        }
    models = {"primary": entries.get("primary"), "secondary": entries.get("secondary")}
    if not entries:
        return {"label": "nsfw", "confidence": 0.0, "agreement": "single",
                "models": models}
    labels = {verdict["label"] for verdict in entries.values()}
    if len(entries) == 1:
        agreement = "single"
    elif len(labels) == 1:
        agreement = "high"
    else:
        agreement = "low"
    label = "nsfw" if "nsfw" in labels else "normal"
    confidence = max(
        verdict["confidence"]
        for verdict in entries.values()
        if verdict["label"] == label
    )
    return {"label": label, "confidence": confidence, "agreement": agreement,
            "models": models}


def score_tags(image_bytes: bytes, tags: list[str], *, threads: int) -> list[float]:
    """Chinese-CLIP image/text cosine similarity, one score per tag.

    One image forward plus one batched text forward; scores are clamped to
    0-1. ``tags`` may be empty, in which case no model is loaded at all.
    """
    if not tags:
        return []
    configure_threads(threads)
    torch = _torch_mod()
    model, processor, _ = _load_clip()
    image = _open_image(image_bytes)
    text_inputs = processor(
        text=list(tags), return_tensors="pt", padding=True, truncation=True
    )
    image_inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        text_out = model.get_text_features(**text_inputs)
        image_out = model.get_image_features(**image_inputs)
        text_features = text_out.pooler_output if hasattr(text_out, "pooler_output") else text_out
        image_features = image_out.pooler_output if hasattr(image_out, "pooler_output") else image_out
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        sims = (image_features @ text_features.T)[0]
    scores = []
    for value in sims:
        number = float(value)
        if not math.isfinite(number):
            number = 0.0
        scores.append(round(min(1.0, max(0.0, number)), 4))
    return scores


def _verification(
    score_map: dict[str, float | None], threshold: float
) -> dict[str, Any]:
    """The ``verification`` block: model, threshold, mean and min score.

    ``None`` entries are unconfirmed tags: they are excluded from the
    statistics rather than counted as a zero.
    """
    model_id = _clip_bundle[2] if _clip_bundle is not None else clip_model_id()
    values = [
        float(value) for value in score_map.values()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    return {
        "model": model_id,
        "threshold": float(threshold),
        "mean_score": round(sum(values) / len(values), 4) if values else None,
        "min_score": round(min(values), 4) if values else None,
    }


def _score_candidates(tags: list[str]) -> list[str]:
    """Tags that go through CLIP: the verdict markers are exempt.

    ``nsfw`` and ``sfw`` come from the classifiers, not from Qwen (whose
    raw output is stripped of both before scoring), so CLIP must never get
    a vote that could threshold either verdict away.  The filter is
    unconditional -- raw model output no longer carries these words, so
    this is belt-and-braces for every caller.
    """
    return [
        tag for tag in tags
        if str(tag).lower() not in sidecar.CLASSIFICATION_TAGS
    ]


def generate_tags(image_bytes: bytes, *, threads: int) -> str:
    """Run the resident fp32 Qwen2.5-VL model and return raw Chinese text."""
    configure_threads(threads)
    torch = _torch_mod()
    model, processor = _load_vlm()
    image = _open_image(image_bytes)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": vlm_prompt()},
            ],
        }
    ]
    prompt_text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = processor(text=[prompt_text], images=[image], return_tensors="pt")
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            use_cache=True,
        )
    generated = output[:, inputs["input_ids"].shape[1]:]
    return processor.batch_decode(generated, skip_special_tokens=True)[0].strip()


def split_tags(tag_part: str) -> list[str]:
    """Split a raw tag line on 、/,/whitespace and drop empty/quoted bits."""
    if not tag_part:
        return []
    parts = _TAG_SPLIT.split(tag_part)
    return [
        part.strip(_TAG_QUOTES)
        for part in parts
        if part.strip(_TAG_QUOTES)
    ]


def parse_model_output(text: str) -> tuple[str, list[str]]:
    """Parse the two-line prompt output into ``(description, tags)``.

    Tolerates swapped lines, inline markers, alternate separators, extra
    whitespace, and refusal/free-form text (kept as the description with an
    empty tag list).
    """
    text = (text or "").strip()
    if not text:
        return "", []

    desc_match = _DESC_MARKER.search(text)
    tags_match = _TAGS_MARKER.search(text)

    description = ""
    tag_part = ""

    if tags_match is not None:
        tag_end = len(text)
        if desc_match is not None and desc_match.start() > tags_match.start():
            tag_end = desc_match.start()
        tag_part = text[tags_match.end():tag_end]

    if desc_match is not None:
        desc_end = len(text)
        if tags_match is not None and tags_match.start() > desc_match.start():
            desc_end = tags_match.start()
        description = text[desc_match.end():desc_end]
    elif tags_match is not None:
        description = text[:tags_match.start()]
    else:
        description = text

    description = next(
        (line.strip() for line in description.splitlines() if line.strip()), ""
    )
    return description, split_tags(tag_part)


def assemble_result(
    raw_text: str,
    nsfw: dict[str, Any],
    *,
    tag_scores: dict[str, float] | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Combine VLM text and NSFW output into the sidecar-ready result.

    Without ``tag_scores`` every clean tag is kept (legacy behavior). With
    them the candidate tags are cut at ``threshold``: kept tags preserve
    their original order, the rest move to ``dropped_tags`` with their
    scores recorded in ``tag_scores``, and the ``nsfw`` marker is applied
    last so the classifier verdict can never be thresholded away.
    """
    description, candidates = parse_model_output(raw_text)
    candidates = sidecar.normalize_candidate_tags(sidecar.clean_tags(candidates))
    nsfw_record = dict(nsfw) if isinstance(nsfw, dict) else {}
    nsfw_label = nsfw_record.get("label")
    if tag_scores is None or threshold is None:
        return {
            "nsfw": nsfw_record,
            "description": description,
            "tags": sidecar.apply_nsfw_tag(candidates, nsfw_label),
        }
    scored = _score_candidates(candidates)
    score_map: dict[str, float | None] = {}
    for tag in scored:
        value = tag_scores.get(tag) if isinstance(tag_scores, dict) else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            # Unconfirmed (missing/dirty score): None is dropped by
            # partition_tags at *any* threshold, so a zero cutoff can never
            # keep a tag that CLIP never scored. Matches the direct
            # partition_tags behavior.
            score_map[tag] = None
        else:
            score_map[tag] = round(min(1.0, max(0.0, float(value))), 4)
    kept, dropped = sidecar.partition_tags(scored, score_map, threshold)
    return {
        "nsfw": nsfw_record,
        "description": description,
        "tags": sidecar.apply_nsfw_tag(kept, nsfw_label),
        "tag_scores": score_map,
        "dropped_tags": dropped,
        "verification": _verification(score_map, threshold),
    }


def tag_image(
    image_bytes: bytes, *, threads: int, threshold: float | None = None
) -> dict[str, Any]:
    """Full per-image pipeline: dual NSFW, VLM tagging, CLIP confirmation.

    The second NSFW classifier is optional (its failure only downgrades the
    record to ``agreement: single``); a CLIP failure propagates so a new
    image is recorded as ``failed`` instead of shipping unconfirmed tags.
    """
    if threshold is None:
        threshold = sidecar.DEFAULT_CLIP_THRESHOLD
    primary = classify_nsfw(image_bytes, threads=threads)
    nsfw = combine_nsfw(primary, _secondary_nsfw(image_bytes, threads))
    raw_text = generate_tags(image_bytes, threads=threads)
    # Parse once here so the CLIP batch covers exactly the candidate tags
    # assemble_result() will partition (both calls are pure and identical).
    # Normalization happens *before* scoring, so CLIP scores the canonical
    # forms and every tag_scores/dropped_tags key matches the written tags.
    _, candidates = parse_model_output(raw_text)
    candidates = sidecar.normalize_candidate_tags(sidecar.clean_tags(candidates))
    scored = _score_candidates(candidates)
    score_map = dict(zip(scored, score_tags(image_bytes, scored, threads=threads)))
    return assemble_result(
        raw_text, nsfw, tag_scores=score_map, threshold=threshold
    )


def verify_image(
    image_bytes: bytes,
    *,
    tags: list[str],
    threads: int,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Verification-only upgrade for an already-tagged (v1) sidecar.

    Dual NSFW classification plus CLIP scoring of the existing tags; the Qwen
    description/tags are never regenerated here. Every model error propagates
    so the caller can leave the original sidecar untouched.
    """
    if threshold is None:
        threshold = sidecar.DEFAULT_CLIP_THRESHOLD
    primary = classify_nsfw(image_bytes, threads=threads)
    nsfw = combine_nsfw(primary, _secondary_nsfw(image_bytes, threads))
    candidates = sidecar.normalize_candidate_tags(sidecar.clean_tags(tags))
    scored = _score_candidates(candidates)
    score_map = dict(zip(scored, score_tags(image_bytes, scored, threads=threads)))
    kept, dropped = sidecar.partition_tags(scored, score_map, threshold)
    return {
        "nsfw": nsfw,
        "tags": sidecar.apply_nsfw_tag(kept, nsfw.get("label")),
        "tag_scores": score_map,
        "dropped_tags": dropped,
        "verification": _verification(score_map, threshold),
    }
