"""Sidecar conventions, safe path checks, and tag hygiene for the tag worker.

Standard library only: repository-root tests import this module in an
environment that has no torch or transformers installed.
"""

from __future__ import annotations

import json
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
SIDECAR_VERSION = 1

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


def should_process(sidecar: Path, now: float | None = None) -> bool:
    """Decide whether an image still needs tagging (skip-vs-retry policy)."""
    if not sidecar.exists():
        return True
    state = read_sidecar(sidecar)
    if state is None:
        return True  # corrupt or undecodable -> rewrite
    status = state.get("status")
    if status == "ok":
        return False
    if status == "failed":
        try:
            attempts = int(state.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= MAX_ATTEMPTS:
            return False  # permanent skip until the sidecar is deleted
        try:
            mtime = sidecar.stat().st_mtime
        except OSError:
            return True
        if now is None:
            now = time.time()
        if now - mtime < RETRY_COOLDOWN_SECONDS:
            return False  # still inside the 30-minute cooldown
        return True
    return True  # unknown status -> rewrite


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
    """Shape a successful inference result into the versioned sidecar."""
    nsfw = result.get("nsfw") or {}
    tags = result.get("tags") or []
    return {
        "version": SIDECAR_VERSION,
        "source": source,
        "generated_at": _now_iso(),
        "status": "ok",
        "model": model,
        "nsfw": {
            "label": str(nsfw.get("label", "")),
            "confidence": float(nsfw.get("confidence", 0.0)),
        },
        "description": str(result.get("description") or ""),
        "tags": [str(tag) for tag in tags][:MAX_TAGS],
    }


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
    """Insert ``nsfw`` as the first tag when the classifier flags the image."""
    label = str(nsfw_label or "").strip().lower()
    if label != "nsfw":
        return list(tags)
    rest = [str(tag) for tag in tags if str(tag).lower() != "nsfw"]
    return (["nsfw"] + rest)[:MAX_TAGS]
