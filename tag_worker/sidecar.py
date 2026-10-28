"""Sidecar conventions, safe path checks, and tag hygiene for the tag worker.

Standard library only: repository-root tests import this module in an
environment that has no torch or transformers installed.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# ``<full filename>.tags.json`` lives next to its source image, e.g.
# ``a.jpg.tags.json``. Gallery/dedup/thumbnail/viewer walks filter on
# IMAGE_EXTS, so these files are invisible to file_browser.py.
SIDECAR_SUFFIX = ".tags.json"
IMAGE_EXTS = frozenset({".webp", ".jpg", ".jpeg", ".png", ".gif"})
LOCK_DIR_NAME = ".media_locks"

# A failed sidecar is retried after a cooldown, at most MAX_ATTEMPTS times,
# then skipped until the operator deletes the sidecar.
MAX_ATTEMPTS = 3
RETRY_COOLDOWN_SECONDS = 30 * 60

# Orphan sidecars (source image gone) are removed only after this age.
ORPHAN_TTL_SECONDS = 60 * 60

MAX_TAGS = 10
MAX_TAG_CHARS = 16

# v1: Qwen description/tags plus one NSFW label.
# v2: adds CLIP confidence confirmation (``tag_scores``/``dropped_tags``/
# ``verification``) and dual-NSFW cross-validation (``nsfw.agreement`` and
# ``nsfw.models``). Both versions are worker-owned by default; the one
# exception is the file browser's manual tag-edit routes, which write the
# same schema with ``manual: true`` and are then treated as read-only here
# (except that orphan cleanup still owns them: known version + source).
SIDECAR_VERSION = 2
KNOWN_SIDECAR_VERSIONS = frozenset({1, SIDECAR_VERSION})

# NSFW agreement levels written by the two-classifier cross-check.
NSFW_AGREEMENTS = frozenset({"high", "low", "single"})

# CLIP cosine cutoff used to keep or drop a proposed tag. Calibrated on the
# production gallery (183 sidecars / 1047 tags: min 0.264, median 0.361):
# 0.30 trims 2.3% of tags (~1.1% once the exempt nsfw marker is excluded),
# while 0.35 would trim 36.6% and start cutting plausible tags. Tunable per
# deployment with TAG_WORKER_CLIP_THRESHOLD; changing it re-cuts existing
# sidecars from their stored scores without loading any model.
DEFAULT_CLIP_THRESHOLD = 0.30

# What an image still needs; ``should_process`` is the skip-vs-work view.
ACTION_SKIP = "skip"            # nothing to do this round
ACTION_TAG = "tag"              # full pipeline (NSFW + Qwen + CLIP)
ACTION_VERIFY = "verify"        # v1 -> v2 upgrade: NSFW + CLIP only, no Qwen
ACTION_REPARTITION = "repartition"  # v2 re-cut at a new threshold, no models

_TAG_NOISE = " \t、,，;；:：\"'“”‘’"


def sidecar_path_for(image_path: str | Path) -> Path:
    """Return ``<full filename>.tags.json`` beside the source image."""
    return Path(f"{image_path}{SIDECAR_SUFFIX}")


def source_path_for(sidecar: str | Path) -> Path:
    """Inverse of :func:`sidecar_path_for`."""
    sidecar = Path(sidecar)
    name = sidecar.name
    if not name.endswith(SIDECAR_SUFFIX):
        raise ValueError(f"not a tag sidecar: {name}")
    return sidecar.with_name(name[: -len(SIDECAR_SUFFIX)])


def is_sidecar_filename(name: str) -> bool:
    """True for worker-owned sidecar names; dot files are never ours."""
    return bool(name) and not name.startswith(".") and name.endswith(SIDECAR_SUFFIX)


def is_worker_temp_filename(name: str) -> bool:
    """Dot-prefixed atomic-write temporaries: ``.<name>.tags.json.<uuid>.tmp``."""
    return (
        bool(name)
        and name.startswith(".")
        and name.endswith(".tmp")
        and f"{SIDECAR_SUFFIX}." in name
    )


def _lexical_norm(path: Path) -> Path:
    return Path(os.path.normpath(str(path)))


def safe_source_path(
    root: Path,
    candidate: Path,
    *,
    require_image: bool = False,
    require_sidecar: bool = False,
    require_temp: bool = False,
) -> Path | None:
    """Validate ``candidate`` against the comics source root, or return None.

    Background-process equivalent of the file-browser comics delete checks
    (lexical containment rejects absolute paths and ``..`` escapes; resolved
    containment rejects symlink escapes). The source root itself, anything
    under ``.media_locks``, non-files, and wrong suffixes are rejected.
    """
    root = Path(root)
    candidate = Path(candidate)
    if not root.is_dir():
        return None

    lexical = candidate if candidate.is_absolute() else root / candidate
    norm_root = _lexical_norm(root)
    norm_lex = _lexical_norm(lexical)
    try:
        norm_lex.relative_to(norm_root)
    except ValueError:
        return None
    if norm_lex == norm_root:
        return None

    try:
        resolved = lexical.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    if resolved == root_resolved:
        return None
    lock_dir = root_resolved / LOCK_DIR_NAME
    if resolved == lock_dir or lock_dir in resolved.parents:
        return None

    if require_image:
        if norm_lex.suffix.lower() not in IMAGE_EXTS:
            return None
        try:
            if not norm_lex.is_file():
                return None
        except OSError:
            return None
    if require_sidecar:
        if not is_sidecar_filename(norm_lex.name):
            return None
        try:
            if norm_lex.is_symlink() or not norm_lex.is_file():
                return None
        except OSError:
            return None
    if require_temp:
        if not is_worker_temp_filename(norm_lex.name):
            return None
        try:
            if norm_lex.is_symlink() or not norm_lex.is_file():
                return None
        except OSError:
            return None
    return norm_lex


def read_sidecar(path: Path) -> dict[str, Any] | None:
    """Parsed sidecar dict, or None when missing/corrupt/not an object."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_worker_sidecar(state: Any) -> bool:
    """True for JSON this worker could have written (known version + source).

    Ownership is a known-version check, not "matches the current version":
    a v1 sidecar is still ours (it just needs the verification upgrade), so
    orphan cleanup may delete it, while any other ``version`` is foreign.
    Manually edited sidecars (``manual: true``) keep the same version and
    source fields, so cleanup still owns them after their image is gone.
    """
    if not isinstance(state, dict):
        return False
    version = state.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version in KNOWN_SIDECAR_VERSIONS and "source" in state


def repartition_needed(state: Any, threshold: float) -> bool:
    """True when a v2 sidecar's stored scores were cut at another threshold.

    Only a complete numeric ``tag_scores`` map can be re-cut without the
    models; anything else (missing verification, partial or dirty scores)
    keeps the tags as written rather than risking a wrong rewrite.
    """
    if not isinstance(state, dict):
        return False
    verification = state.get("verification")
    if not isinstance(verification, dict):
        return False
    recorded = verification.get("threshold")
    if isinstance(recorded, bool) or not isinstance(recorded, (int, float)):
        return False
    recorded = float(recorded)
    if not math.isfinite(recorded) or abs(recorded - float(threshold)) < 1e-9:
        return False  # same threshold: nothing to refresh
    scores = state.get("tag_scores")
    if not isinstance(scores, dict) or not scores:
        return False
    for value in scores.values():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return False
    nsfw = state.get("nsfw")
    label = nsfw.get("label") if isinstance(nsfw, dict) else None
    kept = {
        tag for tag in state.get("tags") or []
        if isinstance(tag, str)
    }
    dropped = {
        tag for tag in state.get("dropped_tags") or []
        if isinstance(tag, str)
    }
    if str(label or "").strip().lower() == "nsfw":
        kept.discard("nsfw")  # classifier marker: exempt from scoring
    return set(scores) == kept | dropped


def process_action(
    sidecar_path: Path,
    now: float | None = None,
    *,
    threshold: float | None = None,
    retry_policy: bool = True,
) -> str:
    """Classify what one image still needs: tag/verify/repartition/skip.

    - ``manual: true`` sidecar -> ``skip`` (dual-write protocol: the file
      browser's manual tag edit wins over every worker path)
    - missing or corrupt sidecar -> ``tag`` (full pipeline)
    - ``status: ok`` at version 1 -> ``verify`` (CLIP + dual-NSFW upgrade,
      the Qwen description/tags are kept as written)
    - ``status: ok`` at version 2 -> ``repartition`` when the recorded
      threshold differs from the current one, otherwise ``skip``
    - ``status: failed`` -> the unchanged cooldown/attempt policy

    The ``manual`` flag is re-read from disk on every call (this function
    never caches a sidecar), so an edit lands for the very next decision.
    It blocks the v1 upgrade, the threshold re-cut, and failed-sidecar
    retries alike; orphan cleanup is unaffected because ownership is still
    a known ``version`` plus ``source``.

    ``retry_policy=False`` keeps the ok-sidecar decisions but always allows
    a failed image through: the round-level scan applies the cooldown, while
    a direct ``process_one`` call keeps the historical "run it now"
    behavior.
    """
    if not sidecar_path.exists():
        return ACTION_TAG
    state = read_sidecar(sidecar_path)
    if state is None:
        return ACTION_TAG  # corrupt or undecodable -> rewrite
    if state.get("manual"):
        # Dual-write protocol: a human edited this sidecar through the
        # gallery's manual tag routes, so it is read-only for the worker --
        # checked before status/version so it also overrides a failed
        # retry and a complete-score re-cut at a changed threshold.
        return ACTION_SKIP
    status = state.get("status")
    if status == "ok":
        version = state.get("version")
        if version == SIDECAR_VERSION:
            if threshold is not None and repartition_needed(state, threshold):
                return ACTION_REPARTITION
            return ACTION_SKIP
        if isinstance(version, int) and not isinstance(version, bool) and version == 1:
            return ACTION_VERIFY
        return ACTION_SKIP  # foreign/unknown version: never touch it
    if status == "failed":
        if not retry_policy:
            return ACTION_TAG
        try:
            attempts = int(state.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= MAX_ATTEMPTS:
            return ACTION_SKIP  # permanent skip until the sidecar is deleted
        try:
            mtime = sidecar_path.stat().st_mtime
        except OSError:
            return ACTION_TAG
        if now is None:
            now = time.time()
        if now - mtime < RETRY_COOLDOWN_SECONDS:
            return ACTION_SKIP  # still inside the 30-minute cooldown
        return ACTION_TAG
    return ACTION_TAG  # unknown status -> rewrite


def should_process(
    sidecar: Path, now: float | None = None, *, threshold: float | None = None
) -> bool:
    """Decide whether an image still needs work (tagging *or* upgrading)."""
    return process_action(sidecar, now, threshold=threshold) != ACTION_SKIP


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a dot-prefixed temp file and ``os.replace``."""
    path = Path(path)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_success_payload(
    source: str, result: dict[str, Any], model: str
) -> dict[str, Any]:
    """Shape a successful inference result into the version 2 sidecar.

    Tolerates results without verification data (``tag_scores`` empty,
    ``dropped_tags`` empty, ``verification`` null) so partial pipelines and
    tests still produce a schema-complete v2 document.
    """
    nsfw = result.get("nsfw") if isinstance(result.get("nsfw"), dict) else {}
    agreement = nsfw.get("agreement")
    if agreement not in NSFW_AGREEMENTS:
        agreement = "single"
    models = nsfw.get("models")
    if not isinstance(models, dict):
        models = None
    dropped = result.get("dropped_tags")
    if not isinstance(dropped, list):
        dropped = []
    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = None
    return {
        "version": SIDECAR_VERSION,
        "source": source,
        "generated_at": _now_iso(),
        "status": "ok",
        "model": model,
        "nsfw": {
            "label": str(nsfw.get("label", "")),
            "confidence": _bounded_float(nsfw.get("confidence"), 0.0),
            "agreement": agreement,
            "models": models,
        },
        "description": str(result.get("description") or ""),
        # MAX_TAGS bounds the Qwen candidates; the injected nsfw marker may
        # add one more, so a full candidate list is never truncated here.
        "tags": [str(tag) for tag in (result.get("tags") or [])][: MAX_TAGS + 1],
        "tag_scores": _score_map(result.get("tag_scores")),
        "dropped_tags": [
            str(tag) for tag in dropped if isinstance(tag, str)
        ][:MAX_TAGS],
        "verification": verification,
    }


def _bounded_float(value: Any, default: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return default
    return float(value)


def _score_map(raw: Any) -> dict[str, float | None]:
    """Validated ``{tag: score}``: finite numbers rounded to 4 decimals.

    An explicitly missing or unusable score stays in the map as ``None``:
    that marks an unconfirmed tag which no threshold may keep, while the
    key is retained so ``set(tag_scores) == tags | dropped_tags`` holds.
    """
    if not isinstance(raw, dict):
        return {}
    scores: dict[str, float | None] = {}
    for tag, value in raw.items():
        if not isinstance(tag, str):
            continue
        number = _bounded_float(value, math.nan)
        scores[tag] = round(number, 4) if math.isfinite(number) else None
    return scores


def partition_tags(
    tags: Any, scores: Any, threshold: float
) -> tuple[list[str], list[str]]:
    """Split candidate tags at the CLIP threshold, preserving input order.

    A tag is kept when its score is >= threshold (the boundary passes) and
    dropped otherwise; a tag without a usable numeric score counts as
    unconfirmed and is dropped. The returned pair is ``(kept, dropped)``.
    """
    kept: list[str] = []
    dropped: list[str] = []
    if not isinstance(tags, (list, tuple)):
        return kept, dropped
    for tag in tags:
        if not isinstance(tag, str):
            continue
        score = scores.get(tag) if isinstance(scores, dict) else None
        if (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and math.isfinite(float(score))
            and float(score) >= float(threshold)
        ):
            kept.append(tag)
        else:
            dropped.append(tag)
    return kept, dropped


def recut_tags(state: dict[str, Any], threshold: float) -> dict[str, Any]:
    """Re-partition a v2 sidecar's stored scores at a new threshold.

    Pure bookkeeping: no model runs, and only ``tags``, ``dropped_tags``,
    ``generated_at`` and the ``verification`` threshold/mean/min change.
    Raises ``ValueError`` when the stored state cannot be re-cut (the
    caller then leaves the file untouched).
    """
    if not repartition_needed(state, threshold):
        raise ValueError("sidecar scores are incomplete or already at threshold")
    scores = _score_map(state.get("tag_scores"))
    nsfw = state.get("nsfw") if isinstance(state.get("nsfw"), dict) else {}
    nsfw_label = nsfw.get("label")
    marker = str(nsfw_label or "").strip().lower() == "nsfw"
    # ``tag_scores`` keeps the original candidate order, so the re-cut
    # preserves the Qwen tag order even for tags that move between buckets.
    candidates = [
        tag for tag in scores
        if not (marker and str(tag).lower() == "nsfw")
    ]
    kept, dropped = partition_tags(candidates, scores, threshold)
    if marker:
        kept = apply_nsfw_tag(kept, nsfw_label)
    values = [float(scores[tag]) for tag in candidates]
    payload = dict(state)
    payload["version"] = SIDECAR_VERSION
    payload["status"] = "ok"
    payload["tags"] = kept
    payload["dropped_tags"] = dropped
    payload["generated_at"] = _now_iso()
    payload["verification"] = {
        **(dict(state.get("verification"))
           if isinstance(state.get("verification"), dict) else {}),
        "threshold": float(threshold),
        "mean_score": round(sum(values) / len(values), 4) if values else None,
        "min_score": round(min(values), 4) if values else None,
    }
    return payload


def build_failure_payload(
    source: str, attempts: int, error: BaseException | str
) -> dict[str, Any]:
    """Shape a retryable failure into the versioned sidecar."""
    if isinstance(error, BaseException):
        message = f"{type(error).__name__}: {error}"
    else:
        message = str(error)
    return {
        "version": SIDECAR_VERSION,
        "source": source,
        "generated_at": _now_iso(),
        "status": "failed",
        "attempts": int(attempts),
        "error": message[:500],
    }


def clean_tags(tags: Any) -> list[str]:
    """Strip, drop noise, de-duplicate, and bound tags (<=16 chars, <=10)."""
    if not isinstance(tags, (list, tuple, set)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = re.sub(r"\s+", " ", raw).strip(_TAG_NOISE)
        if not tag or not re.search(r"\w", tag):
            continue  # empty or punctuation-only
        tag = tag[:MAX_TAG_CHARS]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= MAX_TAGS:
            break
    return cleaned


def apply_nsfw_tag(tags: list[str], nsfw_label: Any) -> list[str]:
    """Insert ``nsfw`` as the first tag when the classifier flags the image.

    The marker is prepended without truncating confirmed candidates: a full
    ``MAX_TAGS`` candidate list stays intact (result cap ``MAX_TAGS + 1``),
    so no passing tag can vanish from *both* ``tags`` and ``dropped_tags``
    and silently break the repartition completeness check. ``MAX_TAGS``
    still bounds the Qwen candidate cleaning in :func:`clean_tags`.
    """
    label = str(nsfw_label or "").strip().lower()
    if label != "nsfw":
        return list(tags)
    rest = [str(tag) for tag in tags if str(tag).lower() != "nsfw"]
    return (["nsfw"] + rest)[: MAX_TAGS + 1]
