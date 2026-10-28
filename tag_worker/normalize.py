"""Versioned closed tag vocabulary for the comics tag worker.

The vocabulary is the plain repository data file ``vocabulary.json`` next
to this module: a hand-edited word list grouped into categories plus an
``aliases`` map (concrete/specific form -> canonical components), with a
``version`` integer as its maintenance entry point.  Editing the file and
bumping ``version`` *is* the promotion workflow -- every writer stamps
the version it normalized against, so existing sidecars become pending
and are refreshed by the model-free upgrade path.

``normalize_tags`` expands alias entries (recursively, order preserved,
de-duplicated) and keeps every other word verbatim: out-of-vocabulary
words are never dropped, they simply stay outside the closed list.
Standard library only, no model is ever loaded here, and a missing or
broken file degrades to an empty vocabulary (everything stays outside,
``vocab_version()`` returns 0) with a warning instead of raising.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("tag_worker.normalize")

# ``tag_worker/vocabulary.json`` sits beside this module in every layout:
# the checkout (``<root>/tag_worker/``), the worker image
# (``/app/tag_worker/``, copied by ``tag_worker/Dockerfile``), and the
# file-browser image (``/app/tag_worker/``, via the root ``COPY . .``) --
# the file browser locates the same file as
# ``Path(file_browser.__file__).parent / "tag_worker" / "vocabulary.json"``.
DEFAULT_VOCABULARY_PATH = Path(__file__).resolve().parent / "vocabulary.json"

# ``{resolved path: (mtime_ns | None, parsed)}``.  ``None`` caches the
# missing-file fallback until the file appears; the lock guards both the
# cache and the single parse so concurrent callers share one view.
_cache_lock = threading.Lock()
_cache: dict[str, tuple[int | None, dict[str, Any]]] = {}


def _empty_vocabulary() -> dict[str, Any]:
    """A version-0 vocabulary: no words, no aliases, normalization is a no-op."""
    return {
        "version": 0,
        "categories": {},
        "aliases": {},
        "words": (),
        "word_set": frozenset(),
        "alias_map": {},
    }


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _parse(raw: Any) -> dict[str, Any] | None:
    """Validate a parsed document; None when it is not a usable vocabulary."""
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    categories = raw.get("categories")
    aliases = raw.get("aliases")
    if not isinstance(categories, dict) or not isinstance(aliases, dict):
        return None
    for name, items in categories.items():
        if not isinstance(name, str) or not _is_str_list(items):
            return None
    for key, components in aliases.items():
        if not isinstance(key, str) or not _is_str_list(components):
            return None
    return {
        "version": version,
        "categories": {
            str(name): list(items) for name, items in categories.items()
        },
        "aliases": {
            str(key): list(components) for key, components in aliases.items()
        },
    }


def _finalize(parsed: dict[str, Any]) -> dict[str, Any]:
    """Derive the ordered word list and the validated alias map."""
    words: list[str] = []
    seen: set[str] = set()
    for items in parsed["categories"].values():
        for item in items:
            if item not in seen:
                seen.add(item)
                words.append(item)
    word_set = frozenset(words)
    alias_map: dict[str, list[str]] = {}
    if word_set:
        for key, components in parsed["aliases"].items():
            missing = [c for c in components if c not in word_set]
            if missing:
                # Drafting rule: every expanded component must be a canonical
                # word.  A hand edit that violates it keeps the original tag
                # (outside-vocabulary preservation) instead of guessing.
                log.warning(
                    "tag vocabulary alias %r ignored: components outside the "
                    "word list: %s",
                    key, ", ".join(missing),
                )
                continue
            alias_map[key] = list(components)
    return {
        "version": parsed["version"],
        "categories": parsed["categories"],
        "aliases": parsed["aliases"],
        "words": tuple(words),
        "word_set": word_set,
        "alias_map": alias_map,
    }


def _load_uncached(target: Path) -> dict[str, Any]:
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        log.warning(
            "tag vocabulary %s unusable (%s: %s); falling back to an empty "
            "vocabulary (every tag stays outside and is preserved)",
            target, type(exc).__name__, exc,
        )
        return _empty_vocabulary()
    parsed = _parse(raw)
    if parsed is None:
        log.warning(
            "tag vocabulary %s is invalid (expected version >= 1 with "
            "categories/aliases string maps); falling back to an empty "
            "vocabulary",
            target,
        )
        return _empty_vocabulary()
    return _finalize(parsed)


def load_vocabulary(path: Path | str | None = None) -> dict[str, Any]:
    """Read ``vocabulary.json`` with an mtime-checked module-level cache.

    Returns ``version`` (0 when the file is missing/broken), ``categories``,
    ``aliases`` plus the derived ``words``/``word_set``/``alias_map`` views.
    Thread safe; a file change is picked up on the next call (mtime), and
    no failure mode raises -- a bad file is an empty vocabulary.
    """
    target = DEFAULT_VOCABULARY_PATH if path is None else Path(path)
    try:
        key = str(target.resolve())
    except OSError:  # pragma: no cover - exotic paths only
        key = str(target)
    try:
        mtime: int | None = target.stat().st_mtime_ns
    except OSError:
        mtime = None  # missing file: cached until it appears
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        data = _load_uncached(target)
        _cache[key] = (mtime, data)
        return data


def vocab_version(path: Path | str | None = None) -> int:
    """Current vocabulary version; 0 when no usable vocabulary is loaded."""
    return int(load_vocabulary(path)["version"])


def vocabulary_words(path: Path | str | None = None) -> list[str]:
    """Every canonical word in category order (deduplicated)."""
    return list(load_vocabulary(path)["words"])


def _expand(word: str, alias_map: dict[str, list[str]],
            stack: frozenset[str] | None = None) -> list[str]:
    """One word -> its canonical components (alias-first, cycle guarded)."""
    components = alias_map.get(word)
    if not components:
        return [word]
    if stack is not None and word in stack:
        # Cyclic alias data can only come from a hand edit; keep the
        # original word rather than recursing forever.
        return [word]
    here = frozenset() if stack is None else stack | {word}
    expanded: list[str] = []
    for component in components:
        expanded.extend(_expand(component, alias_map, here))
    return expanded or [word]


def normalize_tags(tags: Any, path: Path | str | None = None) -> list[str]:
    """Normalize a tag list against the vocabulary.

    Per word: an alias expands into its canonical components (each of
    which may expand further), any other word -- vocabulary member or
    not -- is kept verbatim.  The result is flattened, de-duplicated in
    first-seen order, and never truncated or invented; with an empty
    vocabulary only the de-duplication still applies.
    """
    if not isinstance(tags, (list, tuple)):
        return []
    alias_map = load_vocabulary(path)["alias_map"]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        for word in _expand(raw, alias_map):
            if word not in seen:
                seen.add(word)
                normalized.append(word)
    return normalized
