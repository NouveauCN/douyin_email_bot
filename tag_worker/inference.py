"""Model pipeline for the comics tag worker (NSFW check + Chinese tags).

torch, transformers, and Pillow are imported lazily on first inference and
cached in module-level variables, so repository tests can import this module
without any of them installed. Only standard-library imports happen at module
scope.
"""

from __future__ import annotations

import io
import logging
import math
import re
import time
from typing import Any

from . import sidecar

log = logging.getLogger("tag_worker.inference")

NSFW_MODEL = "Falconsai/nsfw_image_detection"
VLM_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
MAX_NEW_TOKENS = 192
# The only classifier labels that may be trusted verbatim in a sidecar.
_NSFW_LABELS = frozenset({"normal", "nsfw"})

PROMPT = (
    "请描述这张图片，并给出标签。严格按下面两行格式输出，不要输出其他内容：\n"
    "描述：<一句中文描述>\n"
    "标签：<5到10个中文标签，用、分隔>"
)

_DESC_MARKER = re.compile(r"描述\s*[:：]")
_TAGS_MARKER = re.compile(r"标签\s*[:：]")
_TAG_SPLIT = re.compile(r"[、,，;；/|\s。]+")
_TAG_QUOTES = "\"'“”‘’"

# Lazy caches: populated on first inference, never at import time.
_torch = None
_nsfw_bundle = None
_vlm_bundle = None


def _torch_mod():
    global _torch
    if _torch is None:
        import torch

        _torch = torch
    return _torch


def configure_threads(threads: int) -> None:
    """Apply TAG_WORKER_THREADS to torch (called on both model paths)."""
    _torch_mod().set_num_threads(max(1, int(threads)))


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


def classify_nsfw(image_bytes: bytes, *, threads: int) -> dict[str, Any]:
    """Classify one image; unusable model output is recorded as ``nsfw``."""
    configure_threads(threads)
    torch = _torch_mod()
    processor, model = _load_nsfw()
    image = _open_image(image_bytes)
    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        probs = model(**inputs).logits.softmax(dim=-1)[0]
    idx = int(probs.argmax())
    label = str(model.config.id2label.get(idx, "") or "").strip().lower()
    confidence = float(probs[idx])
    if (
        label not in _NSFW_LABELS
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        # Unusable output (unexpected label, non-finite confidence): stay
        # conservative so the sidecar still carries the nsfw tag instead of
        # silently trusting garbage.
        log.warning("NSFW model returned an unusable result; recording nsfw")
        return {"label": "nsfw", "confidence": 0.0}
    return {"label": label, "confidence": round(confidence, 4)}


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
                {"type": "text", "text": PROMPT},
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


def assemble_result(raw_text: str, nsfw: dict[str, Any]) -> dict[str, Any]:
    """Combine VLM text and NSFW output into the sidecar-ready result."""
    description, tags = parse_model_output(raw_text)
    tags = sidecar.clean_tags(tags)
    tags = sidecar.apply_nsfw_tag(tags, nsfw.get("label"))
    return {
        "nsfw": dict(nsfw),
        "description": description,
        "tags": tags,
    }


def tag_image(image_bytes: bytes, *, threads: int) -> dict[str, Any]:
    """Full per-image pipeline: NSFW classification, then VLM tagging."""
    nsfw = classify_nsfw(image_bytes, threads=threads)
    raw_text = generate_tags(image_bytes, threads=threads)
    return assemble_result(raw_text, nsfw)
