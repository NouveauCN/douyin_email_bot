"""Long-running single-process scanner/tagger for the comics gallery.

The loop is session-based: a cheap round only stats sidecars to find pending
work, and only when work exists (and no maintenance window is open) does a
session lazily load the models, drain the pending set, and unload them again
-- between sessions and during maintenance windows the process holds no model
memory at all. Images are read through a fully-read file descriptor and
written as one atomic ``<name>.tags.json`` sidecar per image. Never modifies
media bytes and never takes the cooperative media lock; see ARCHITECTURE.md
"Media coordination".
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import inference, normalize, sidecar

log = logging.getLogger("tag_worker")

DEFAULT_ROOT = "/app/comics/pics"
DEFAULT_NICE = 15
DEFAULT_THREADS = 4
DEFAULT_SCAN_INTERVAL = 60.0
DEFAULT_ITEM_DELAY = 1.0

# Maintenance windows (HH:MM-HH:MM, comma separated, may cross midnight).
# They bracket the systemd user timers maa-daily (01:00/09:00/17:00) and
# maa-check (01:35) by 30 minutes; empty input disables them.
DEFAULT_MAINT_WINDOWS = "00:30-01:45,08:30-09:45,16:30-17:45"
# While a window defers work the loop polls this often instead of waiting a
# full scan interval, so the next session starts right after the window ends.
MAINT_POLL_SECONDS = 30.0

# Failure-sidecar writes that themselves failed: sidecar path -> attempts.
# Keeps the MAX_ATTEMPTS cap effective when attempts cannot be persisted
# (otherwise every 60s round would re-run inference forever). Cleared on
# process restart, on the next successful write, or as soon as a sidecar at
# that path becomes readable again so the persisted policy takes over.
_write_failures: dict[str, int] = {}

# Verification-upgrade (v1 -> v2) failures per sidecar path. The original
# sidecar is never overwritten on failure, so this in-memory counter is the
# only thing that stops a broken CLIP/second-NSFW model from retrying every
# round: after MAX_ATTEMPTS the path is deferred until restart.
_upgrade_failures: dict[str, int] = {}

# The "maintenance window active" line is logged once per window, not once
# per poll.
_window_wait_logged = False


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw.strip()) if raw.strip() else default
    except ValueError:
        log.warning("invalid %s=%r; using %s", name, raw, default)
        value = default
    return max(low, min(high, value))


def _env_float(name: str, default: float, low: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw.strip()) if raw.strip() else default
    except ValueError:
        log.warning("invalid %s=%r; using %s", name, raw, default)
        value = default
    return max(low, value)


def _parse_hhmm(text: str) -> int:
    """``HH:MM`` -> minutes since midnight; raises ValueError when invalid."""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"not HH:MM: {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"out of range: {text!r}")
    return hour * 60 + minute


def parse_maint_windows(raw: str | None) -> list[tuple[int, int]]:
    """Parse ``HH:MM-HH:MM`` segments into ``(start, end)`` minute pairs.

    Empty input disables maintenance windows; a malformed segment is skipped
    with a warning so a typo can never crash the service. ``start`` may be
    later than ``end`` for a window that crosses midnight. Boundaries are
    start-inclusive and end-exclusive.
    """
    windows: list[tuple[int, int]] = []
    for segment in str(raw if raw is not None else "").split(","):
        segment = segment.strip()
        if not segment:
            continue
        try:
            start_text, end_text = segment.split("-", 1)
            start = _parse_hhmm(start_text)
            end = _parse_hhmm(end_text)
        except ValueError as exc:
            log.warning(
                "ignoring malformed TAG_WORKER_MAINT_WINDOWS segment %r (%s)",
                segment, exc,
            )
            continue
        if start == end:
            log.warning(
                "ignoring zero-length TAG_WORKER_MAINT_WINDOWS segment %r",
                segment,
            )
            continue
        windows.append((start, end))
    return windows


def in_maint_window(
    windows: list[tuple[int, int]] | None, now_minutes: int | None = None
) -> bool:
    """True inside any maintenance window at ``now_minutes`` (local time).

    Start is inclusive, end exclusive, so a window ending at 01:45 does not
    block the minute it hands back to.
    """
    if not windows:
        return False
    if now_minutes is None:
        local = time.localtime()
        now_minutes = local.tm_hour * 60 + local.tm_min
    for start, end in windows:
        if start <= end:
            if start <= now_minutes < end:
                return True
        elif now_minutes >= start or now_minutes < end:
            return True  # crosses midnight
    return False


def round_sleep(stats: dict, scan_interval: float) -> float:
    """Seconds until the next round: poll fast while a window defers work."""
    if stats.get("paused", 0) > 0:
        return MAINT_POLL_SECONDS
    return scan_interval


def apply_nice(nice: int) -> None:
    """Lower priority once at startup (best effort; containers may lack it)."""
    if not hasattr(os, "nice"):
        return
    try:
        os.nice(int(nice))
    except (OSError, ValueError) as exc:
        log.warning("os.nice(%s) failed: %s", nice, exc)


def scan_images(root: Path) -> list[Path]:
    """Collect image files below ``root``, newest mtime first.

    Recursive walk without following symlinks; dot files and dot directories
    (upload temporaries, ``.media_locks``) and non-``_IMAGE_EXTS`` names
    (which includes every ``*.tags.json`` sidecar) are skipped.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    try:
        root_resolved = root.resolve()
    except OSError:
        return []

    entries: list[tuple[float, str, Path]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(directory) / name
            if path.suffix.lower() not in sidecar.IMAGE_EXTS:
                continue
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                path.resolve().relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            entries.append((-stat.st_mtime, name, path))
    entries.sort()
    return [path for _, _, path in entries]


def cleanup_orphans(root: Path, now: float | None = None) -> int:
    """Delete worker-owned sidecars whose source image is gone for >1 hour.

    Only ``*.tags.json`` files inside the comics source are considered, and
    only when the JSON matches this worker's schema (a known ``version`` plus
    ``source`` -- v1 and v2 are both ours, any other version is foreign), so
    foreign ``*.tags.json`` files are never deleted. This ownership rule is
    deliberately independent of the ``manual: true`` flag: a manually edited
    sidecar is read-only for tagging/upgrades/retries, but once its image is
    gone it is cleaned like any other worker-owned sidecar. Images,
    directories, ``.media_locks``, dot files, and anything outside the source
    are never touched. The path goes through the same lexical+resolve
    validation as every other worker path.
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    if now is None:
        now = time.time()

    removed = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if not sidecar.is_sidecar_filename(name):
                continue
            path = Path(directory) / name
            if sidecar.safe_source_path(root, path, require_sidecar=True) is None:
                continue
            state = sidecar.read_sidecar(path)
            if not sidecar.is_worker_sidecar(state):
                continue  # foreign or corrupt: not ours, never delete
            if sidecar.source_path_for(path).exists():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime <= sidecar.ORPHAN_TTL_SECONDS:
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("could not remove orphan sidecar %s: %s", path, exc)
                continue
            removed += 1
            log.info("removed orphan sidecar %s", path.name)
    return removed


def cleanup_stale_temps(root: Path, now: float | None = None) -> int:
    """Recycle crash leftovers: ``.<name>.tags.json.<uuid>.tmp`` older than 1h.

    Same scope and validation as orphan cleanup: comics source only, never
    ``.media_locks``, never dot directories, never symlinks or paths that
    resolve outside the source.
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    if now is None:
        now = time.time()

    removed = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if not sidecar.is_worker_temp_filename(name):
                continue
            path = Path(directory) / name
            if sidecar.safe_source_path(root, path, require_temp=True) is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime <= sidecar.ORPHAN_TTL_SECONDS:
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("could not remove stale tag-worker temp %s: %s", name, exc)
                continue
            removed += 1
            log.info("removed stale tag-worker temp %s", name)
    return removed


def _write_backoff_active(sidecar_path: Path) -> bool:
    """True when failure-sidecar writes kept failing for this path.

    The in-memory counter enforces MAX_ATTEMPTS even though nothing could be
    persisted. It clears itself once a sidecar at the path is readable again
    (disk recovered) so the persisted skip/cooldown policy takes over.
    """
    failures = _write_failures.get(str(sidecar_path), 0)
    if failures < sidecar.MAX_ATTEMPTS:
        return False
    if sidecar.read_sidecar(sidecar_path) is not None:
        _write_failures.pop(str(sidecar_path), None)
        return False
    return True


def _upgrade_backoff_active(sidecar_path: Path) -> bool:
    """True after MAX_ATTEMPTS failed no-overwrite upgrades in this process.

    Shared by all three paths that must never destroy the existing
    sidecar (v1 -> v2 verification, threshold re-cut, and vocabulary
    normalization): an in-process cap is the only thing that stops a
    permanent disk/model fault from retrying every round. Unlike the
    write-failure counter this never self-clears, so only a restart
    re-arms the work.
    """
    return _upgrade_failures.get(str(sidecar_path), 0) >= sidecar.MAX_ATTEMPTS


def _count_upgrade_failure(sidecar_path: Path, image_name: str) -> int:
    """Bump the shared no-overwrite failure cap and log the escalation."""
    failures = _upgrade_failures.get(str(sidecar_path), 0) + 1
    _upgrade_failures[str(sidecar_path)] = failures
    if failures >= sidecar.MAX_ATTEMPTS:
        log.warning(
            "further upgrades/re-cuts for %s deferred until restart "
            "(in-memory attempts=%d/%d)",
            image_name, failures, sidecar.MAX_ATTEMPTS,
        )
    return failures


def _recut_one(
    image_path: Path, sidecar_path: Path, state: dict, threshold: float
) -> str:
    """Re-cut a v2 sidecar at the current threshold without any model.

    A failing re-cut (typically a full disk) never rewrites the sidecar and
    shares the in-process MAX_ATTEMPTS cap with the verification path, so a
    permanent fault stops after three tries instead of every round.
    """
    started = time.perf_counter()
    try:
        payload = sidecar.recut_tags(state, threshold)
        sidecar.atomic_write_json(sidecar_path, payload)
    except Exception as exc:  # noqa: BLE001 - keep the original sidecar
        failures = _count_upgrade_failure(sidecar_path, image_path.name)
        log.error(
            "tag re-cut failed for %s (%d/%d); original sidecar kept "
            "error=%s: %s",
            image_path.name, failures, sidecar.MAX_ATTEMPTS,
            type(exc).__name__, exc,
        )
        return "upgrade-failed"
    _upgrade_failures.pop(str(sidecar_path), None)
    log.info(
        "re-cut %s tags at threshold %.3f kept=%d dropped=%d (no models) in %.2fs",
        image_path.name, threshold, len(payload["tags"]),
        len(payload["dropped_tags"]), time.perf_counter() - started,
    )
    return "repartitioned"


def _normalize_one(image_path: Path, sidecar_path: Path, state: dict) -> str:
    """Refresh one sidecar's vocabulary normalization without any model.

    Rewrites only the tag spellings (``tags``/``dropped_tags``/
    ``tag_scores`` keys) plus the ``vocabulary_version`` stamp and keeps
    every other field -- ``manual`` markers included.  Shares the
    in-process MAX_ATTEMPTS no-overwrite cap with the verification and
    re-cut paths: a failing rewrite (typically a full disk) leaves the
    original sidecar intact and stops retrying after three tries.
    """
    started = time.perf_counter()
    try:
        payload = sidecar.normalize_sidecar(state)
        sidecar.atomic_write_json(sidecar_path, payload)
    except Exception as exc:  # noqa: BLE001 - never destroy the original
        failures = _count_upgrade_failure(sidecar_path, image_path.name)
        log.error(
            "vocabulary normalization of %s failed (%d/%d); original sidecar "
            "kept error=%s: %s",
            image_path.name, failures, sidecar.MAX_ATTEMPTS,
            type(exc).__name__, exc,
        )
        return "upgrade-failed"
    _upgrade_failures.pop(str(sidecar_path), None)
    log.info(
        "normalized %s to vocabulary v%d kept=%d dropped=%d (no models) in %.3fs",
        image_path.name, payload.get("vocabulary_version", 0),
        len(payload["tags"]), len(payload.get("dropped_tags") or []),
        time.perf_counter() - started,
    )
    return "normalized"


def _verify_one(
    image_path: Path,
    sidecar_path: Path,
    state: dict,
    image_bytes: bytes,
    threads: int,
    threshold: float,
    started: float,
) -> str:
    """Run the v1 -> v2 verification upgrade for one already-tagged image.

    Any failure keeps the original sidecar byte-for-byte and counts against
    the in-process upgrade cap instead of writing a failed sidecar over good
    v1 data.
    """
    previous = state if isinstance(state, dict) else {}
    tags = [
        tag for tag in previous.get("tags") or [] if isinstance(tag, str)
    ]
    try:
        result = inference.verify_image(
            image_bytes, tags=tags, threads=threads, threshold=threshold
        )
        result["description"] = str(previous.get("description") or "")
        payload = sidecar.build_success_payload(
            image_path.name,
            result,
            str(previous.get("model") or inference.VLM_MODEL),
        )
        sidecar.atomic_write_json(sidecar_path, payload)
    except Exception as exc:  # noqa: BLE001 - never overwrite v1 data
        failures = _upgrade_failures.get(str(sidecar_path), 0) + 1
        _upgrade_failures[str(sidecar_path)] = failures
        log.error(
            "verification upgrade of %s failed (%d/%d); original sidecar kept "
            "error=%s: %s",
            image_path.name, failures, sidecar.MAX_ATTEMPTS,
            type(exc).__name__, exc,
        )
        if failures >= sidecar.MAX_ATTEMPTS:
            log.warning(
                "further verification upgrades for %s deferred until restart",
                image_path.name,
            )
        return "upgrade-failed"
    _upgrade_failures.pop(str(sidecar_path), None)
    nsfw = payload["nsfw"]
    log.info(
        "upgraded %s in %.1fs tags=%d dropped=%d nsfw=%s agreement=%s status=ok",
        image_path.name, time.perf_counter() - started, len(payload["tags"]),
        len(payload["dropped_tags"]), nsfw["label"], nsfw["agreement"],
    )
    return "upgraded"


def process_one(image_path: Path, *, threads: int, threshold: float | None = None) -> str:
    """Tag, verify, re-cut, or vocabulary-normalize one image.

    Returns ``ok`` (full pipeline), ``upgraded`` (v1 -> v2 verification),
    ``repartitioned`` (threshold re-cut, no models), ``normalized``
    (vocabulary upgrade, no models and no image read), ``failed``/
    ``read-error``/``backoff`` as before, ``upgrade-failed`` when a
    verification/re-cut/normalization could not run (the original
    sidecar is untouched), or ``skipped`` when there is nothing to do.
    Read failures are logged without a sidecar so the next round retries
    them; inference/write failures accumulate ``attempts`` in the
    sidecar. When the sidecar itself cannot be written, an in-memory
    counter keeps the 3-attempt cap effective.
    """
    if threshold is None:
        threshold = sidecar.DEFAULT_CLIP_THRESHOLD
    sidecar_path = sidecar.sidecar_path_for(image_path)
    if _write_backoff_active(sidecar_path):
        return "backoff"
    action = sidecar.process_action(
        sidecar_path, threshold=threshold, retry_policy=False
    )
    if action == sidecar.ACTION_SKIP:
        return "skipped"
    previous = sidecar.read_sidecar(sidecar_path)
    attempts = 0
    if isinstance(previous, dict) and previous.get("status") == "failed":
        try:
            attempts = int(previous.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
    attempts = max(attempts, _write_failures.get(str(sidecar_path), 0))

    if (
        action in (
            sidecar.ACTION_VERIFY,
            sidecar.ACTION_REPARTITION,
            sidecar.ACTION_NORMALIZE,
        )
        and _upgrade_backoff_active(sidecar_path)
    ):
        return "backoff"

    if action == sidecar.ACTION_NORMALIZE:
        # Vocabulary-only upgrade: sidecar JSON rewrite, no image read and
        # no model load (this returns before any inference call).
        return _normalize_one(
            image_path, sidecar_path,
            previous if isinstance(previous, dict) else {},
        )

    if action == sidecar.ACTION_REPARTITION:
        return _recut_one(
            image_path, sidecar_path,
            previous if isinstance(previous, dict) else {}, threshold,
        )

    started = time.perf_counter()
    try:
        # Open-then-read the whole file: on POSIX the already-open fd stays
        # consistent even if a concurrent delete removes the directory entry.
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        log.warning(
            "read failed for %s (no sidecar; will retry): %s",
            image_path.name, exc,
        )
        return "read-error"

    if action == sidecar.ACTION_VERIFY:
        return _verify_one(
            image_path, sidecar_path, previous, image_bytes,
            threads, threshold, started,
        )

    try:
        result = inference.tag_image(
            image_bytes, threads=threads, threshold=threshold
        )
        payload = sidecar.build_success_payload(
            image_path.name, result, inference.VLM_MODEL
        )
        sidecar.atomic_write_json(sidecar_path, payload)
        _write_failures.pop(str(sidecar_path), None)
    except Exception as exc:  # noqa: BLE001 - long-running service: log, count
        attempts += 1
        log.error(
            "tagged %s in %.1fs status=failed attempts=%d/%d error=%s: %s",
            image_path.name, time.perf_counter() - started, attempts,
            sidecar.MAX_ATTEMPTS, type(exc).__name__, exc,
        )
        try:
            sidecar.atomic_write_json(
                sidecar_path,
                sidecar.build_failure_payload(image_path.name, attempts, exc),
            )
        except Exception as write_exc:  # noqa: BLE001 - count, keep looping
            # Attempts could not be persisted; remember them in-process so
            # the 3-attempt cap still bounds inference for this path.
            _write_failures[str(sidecar_path)] = attempts
            log.error(
                "could not write failure sidecar for %s "
                "(in-memory attempts=%d/%d): %s",
                image_path.name, attempts, sidecar.MAX_ATTEMPTS, write_exc,
            )
        else:
            _write_failures.pop(str(sidecar_path), None)
        return "failed"

    nsfw = payload["nsfw"]
    log.info(
        "tagged %s in %.1fs tags=%d nsfw=%s conf=%.4f agreement=%s status=ok",
        image_path.name, time.perf_counter() - started, len(payload["tags"]),
        nsfw["label"], float(nsfw["confidence"]), nsfw.get("agreement", "single"),
    )
    return "ok"


def _needs_work(image: Path, threshold: float) -> bool:
    """True when one image still needs work (stats only, never loads a model).

    Two in-process caps are folded in so a round never starts a session
    that is doomed to do nothing: failure-sidecar writes that hit the
    MAX_ATTEMPTS cap (nothing could be persisted) and verification/re-cut/
    vocabulary-normalization failures that hit the same cap (the original
    sidecar must not be overwritten). Both clear on restart. Unreadable
    images cannot be detected here, but they fail before any inference, so
    they never trigger a model load either.
    """
    sidecar_path = sidecar.sidecar_path_for(image)
    if _write_backoff_active(sidecar_path):
        return False
    action = sidecar.process_action(sidecar_path, threshold=threshold)
    if action == sidecar.ACTION_SKIP:
        return False
    if (
        action in (
            sidecar.ACTION_VERIFY,
            sidecar.ACTION_REPARTITION,
            sidecar.ACTION_NORMALIZE,
        )
        and _upgrade_backoff_active(sidecar_path)
    ):
        return False
    return True


def _count_status(stats: dict, status: str) -> None:
    if status in ("backoff", "skipped"):
        stats["skipped"] += 1
    elif status == "ok":
        stats["processed"] += 1
    elif status == "upgraded":
        stats["upgraded"] += 1
    elif status == "repartitioned":
        stats["repartitioned"] += 1
    elif status == "normalized":
        stats["normalized"] += 1
    elif status == "upgrade-failed":
        stats["upgrade_failed"] += 1
    elif status == "read-error":
        stats["read_errors"] += 1
    else:
        stats["failed"] += 1


def _fmt_mib(value) -> str:
    return "?" if value is None else f"{float(value):.1f}"


def _drain_session(
    root: Path,
    pending: list[Path],
    stats: dict,
    *,
    threads: int,
    item_delay: float,
    threshold: float,
    maint_windows: list[tuple[int, int]] | None,
) -> None:
    """Drain every pending image, then unload the models -- always.

    Models load lazily on the first image of the session and are dropped in
    ``finally``, so even a crashed session leaves no model memory resident.
    After each image the directory is re-scanned (minus what this session
    already attempted) so images that arrive mid-session join the same batch
    instead of waiting for the next round.
    """
    baseline = (
        stats["processed"] + stats["upgraded"] + stats["repartitioned"]
        + stats["normalized"]
    )
    log.info("session start (pending=%d)", len(pending))
    started = time.perf_counter()
    attempted: set[str] = set()
    queue = list(pending)
    try:
        while queue:
            image = queue.pop(0)
            attempted.add(str(image))
            _count_status(
                stats,
                process_one(image, threads=threads, threshold=threshold),
            )
            if item_delay > 0:
                time.sleep(item_delay)
            queue = [
                candidate for candidate in scan_images(root)
                if str(candidate) not in attempted
                and _needs_work(candidate, threshold)
            ]
            if queue and in_maint_window(maint_windows):
                stats["paused"] += len(queue)
                log.info(
                    "maintenance window reached after %s; deferring %d images "
                    "and unloading models", image.name, len(queue),
                )
                break
    finally:
        unload = inference.unload_models()
        info = unload if isinstance(unload, dict) else {}
        handled = (
            stats["processed"] + stats["upgraded"] + stats["repartitioned"]
            + stats["normalized"]
            - baseline
        )
        log.info(
            "session end (processed=%d, unloaded, rss %s→%s MB, %.1fs)",
            handled,
            _fmt_mib(info.get("rss_before_mib")),
            _fmt_mib(info.get("rss_after_mib")),
            time.perf_counter() - started,
        )


def run_once(
    root: Path,
    *,
    threads: int,
    item_delay: float,
    threshold: float | None = None,
    maint_windows: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """One full round: cheap scan, vocabulary pass, optional model session.

    A round with no pending work loads nothing at all.  The vocabulary
    normalization pass runs right after the scan, before any session is
    considered: it only rewrites sidecar JSON (never reads an image, never
    calls inference), so a round whose only pending work is a vocabulary
    upgrade drains it without opening a session or loading a model -- it
    also keeps running inside maintenance windows, which only ever defer
    *model* work.  Pending model work inside a window is counted
    ``paused`` and deferred (the scan itself still runs); otherwise a
    session drains it and unloads the models before the round returns.
    Round counters are returned for the summary log.
    """
    global _window_wait_logged
    if threshold is None:
        threshold = sidecar.DEFAULT_CLIP_THRESHOLD
    if not in_maint_window(maint_windows):
        _window_wait_logged = False
    stats = {
        "images": 0,
        "processed": 0,
        "upgraded": 0,
        "repartitioned": 0,
        "normalized": 0,
        "skipped": 0,
        "paused": 0,
        "failed": 0,
        "read_errors": 0,
        "upgrade_failed": 0,
        "orphans_cleaned": 0,
        "temps_cleaned": 0,
    }
    images = scan_images(root)
    stats["images"] = len(images)
    stats["orphans_cleaned"] = cleanup_orphans(root)
    stats["temps_cleaned"] = cleanup_stale_temps(root)

    # Model-free vocabulary upgrades: drain them in the cheap-scan phase so
    # they never open a session (and therefore never touch a model loader),
    # regardless of maintenance windows.  Every image this pass touches is
    # excluded from the pending scan below -- a successful rewrite already
    # counted as ``normalized`` and a failed one must not burn a second
    # attempt inside a session in the same round.
    vocab_handled: set[str] = set()
    for image in images:
        image_sidecar = sidecar.sidecar_path_for(image)
        if _write_backoff_active(image_sidecar):
            continue  # nothing could be persisted: the pending scan skips it
        if sidecar.process_action(
            image_sidecar, threshold=threshold
        ) != sidecar.ACTION_NORMALIZE:
            continue
        vocab_handled.add(str(image))
        if _upgrade_backoff_active(image_sidecar):
            _count_status(stats, "backoff")
            continue
        state = sidecar.read_sidecar(image_sidecar)
        if not isinstance(state, dict):
            vocab_handled.discard(str(image))  # vanished mid-round: re-decide
            continue
        _count_status(stats, _normalize_one(image, image_sidecar, state))

    pending: list[Path] = []
    for image in images:
        if str(image) in vocab_handled:
            continue
        if _needs_work(image, threshold):
            pending.append(image)
        else:
            stats["skipped"] += 1

    if not pending:
        return stats  # idle check: no model may be loaded on this path
    if in_maint_window(maint_windows):
        stats["paused"] = len(pending)
        if not _window_wait_logged:
            _window_wait_logged = True
            log.info(
                "maintenance window active: %d pending images deferred "
                "(models stay unloaded, polling every %.0fs)",
                len(pending), MAINT_POLL_SECONDS,
            )
        return stats

    _drain_session(
        root, pending, stats, threads=threads, item_delay=item_delay,
        threshold=threshold, maint_windows=maint_windows,
    )
    return stats


def _maint_windows_setting() -> str:
    """``TAG_WORKER_MAINT_WINDOWS``: unset -> default, empty -> disabled."""
    raw = os.environ.get("TAG_WORKER_MAINT_WINDOWS")
    return DEFAULT_MAINT_WINDOWS if raw is None else raw


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    nice = _env_int("TAG_WORKER_NICE", DEFAULT_NICE, 0, 19)
    threads = _env_int("TAG_WORKER_THREADS", DEFAULT_THREADS, 1, 256)
    scan_interval = _env_float(
        "TAG_WORKER_SCAN_INTERVAL", DEFAULT_SCAN_INTERVAL, 1.0
    )
    item_delay = _env_float("TAG_WORKER_ITEM_DELAY", DEFAULT_ITEM_DELAY, 0.0)
    threshold = _env_float(
        "TAG_WORKER_CLIP_THRESHOLD", sidecar.DEFAULT_CLIP_THRESHOLD, 0.0
    )
    maint_windows = parse_maint_windows(_maint_windows_setting())
    root = Path(os.environ.get("COMICS_PICS_PATH") or DEFAULT_ROOT)

    apply_nice(nice)
    log.info(
        "tag worker starting root=%s threads=%d nice=%d scan_interval=%.1fs "
        "item_delay=%.1fs clip_threshold=%.3f clip_model=%s nsfw2_model=%s "
        "vocab_version=%d maint_windows=%s",
        root, threads, nice, scan_interval, item_delay, threshold,
        inference.clip_model_id(), inference.nsfw2_model_id(),
        normalize.vocab_version(),
        ",".join(
            f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
            for start, end in maint_windows
        ) or "disabled",
    )

    try:
        while True:
            started = time.monotonic()
            stats = None
            try:
                stats = run_once(
                    root, threads=threads, item_delay=item_delay,
                    threshold=threshold, maint_windows=maint_windows,
                )
            except Exception:  # noqa: BLE001 - never exit the long-running loop
                log.exception(
                    "scan round failed; retrying in %.1fs", scan_interval
                )
            if stats is None:
                time.sleep(scan_interval)
                continue
            log.info(
                "round complete images=%d processed=%d upgraded=%d "
                "repartitioned=%d normalized=%d skipped=%d paused=%d failed=%d "
                "read_errors=%d upgrade_failed=%d orphans_cleaned=%d "
                "temps_cleaned=%d elapsed=%.1fs",
                stats["images"], stats["processed"], stats["upgraded"],
                stats["repartitioned"], stats["normalized"], stats["skipped"],
                stats["paused"], stats["failed"], stats["read_errors"],
                stats["upgrade_failed"], stats["orphans_cleaned"],
                stats["temps_cleaned"], time.monotonic() - started,
            )
            time.sleep(round_sleep(stats, scan_interval))
    except KeyboardInterrupt:
        log.info("tag worker stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
