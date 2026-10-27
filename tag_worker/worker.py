"""Long-running single-process scanner/tagger for the comics gallery.

Reads every image through a fully-read file descriptor, runs the NSFW and
VLM models (loaded lazily, kept resident), and writes one atomic
``<name>.tags.json`` sidecar per image. Never modifies media bytes and never
takes the cooperative media lock; see ARCHITECTURE.md "Media coordination".
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import inference, sidecar

log = logging.getLogger("tag_worker")

DEFAULT_ROOT = "/app/comics/pics"
DEFAULT_NICE = 15
DEFAULT_THREADS = 4
DEFAULT_SCAN_INTERVAL = 60.0
DEFAULT_ITEM_DELAY = 1.0

# Failure-sidecar writes that themselves failed: sidecar path -> attempts.
# Keeps the MAX_ATTEMPTS cap effective when attempts cannot be persisted
# (otherwise every 60s round would re-run inference forever). Cleared on
# process restart, on the next successful write, or as soon as a sidecar at
# that path becomes readable again so the persisted policy takes over.
_write_failures: dict[str, int] = {}


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
    only when the JSON is this worker's schema (``version`` + ``source``), so
    foreign ``*.tags.json`` files are never deleted. Images, directories,
    ``.media_locks``, dot files, and anything outside the source are never
    touched. The path goes through the same lexical+resolve validation as
    every other worker path.
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
            if (
                not isinstance(state, dict)
                or state.get("version") != sidecar.SIDECAR_VERSION
                or "source" not in state
            ):
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
                log.warning("could not remove stale temp %s: %s", path, exc)
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


def process_one(image_path: Path, *, threads: int) -> str:
    """Tag one image and write its sidecar.

    Returns ok/failed/read-error/backoff. Read failures are logged without a
    sidecar so the next round retries them; inference/write failures
    accumulate ``attempts`` in the sidecar. When the sidecar itself cannot
    be written, an in-memory counter keeps the 3-attempt cap effective.
    """
    sidecar_path = sidecar.sidecar_path_for(image_path)
    if _write_backoff_active(sidecar_path):
        return "backoff"
    previous = sidecar.read_sidecar(sidecar_path)
    attempts = 0
    if isinstance(previous, dict) and previous.get("status") == "failed":
        try:
            attempts = int(previous.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
    attempts = max(attempts, _write_failures.get(str(sidecar_path), 0))

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

    try:
        result = inference.tag_image(image_bytes, threads=threads)
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
        "tagged %s in %.1fs tags=%d nsfw=%s conf=%.4f status=ok",
        image_path.name, time.perf_counter() - started, len(payload["tags"]),
        nsfw["label"], float(nsfw["confidence"]),
    )
    return "ok"


def run_once(
    root: Path, *, threads: int, item_delay: float
) -> dict[str, int]:
    """One full scan/process/cleanup round; returns round counters."""
    stats = {
        "images": 0,
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "read_errors": 0,
        "orphans_cleaned": 0,
        "temps_cleaned": 0,
    }
    images = scan_images(root)
    stats["images"] = len(images)
    stats["orphans_cleaned"] = cleanup_orphans(root)
    stats["temps_cleaned"] = cleanup_stale_temps(root)

    for image in images:
        if not sidecar.should_process(sidecar.sidecar_path_for(image)):
            stats["skipped"] += 1
            continue
        status = process_one(image, threads=threads)
        if status == "backoff":
            # Inference did not run: failure-sidecar write cap reached.
            stats["skipped"] += 1
            continue
        if status == "ok":
            stats["processed"] += 1
        elif status == "read-error":
            stats["read_errors"] += 1
        else:
            stats["failed"] += 1
        if item_delay > 0:
            time.sleep(item_delay)
    return stats


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
    root = Path(os.environ.get("COMICS_PICS_PATH") or DEFAULT_ROOT)

    apply_nice(nice)
    log.info(
        "tag worker starting root=%s threads=%d nice=%d "
        "scan_interval=%.1fs item_delay=%.1fs",
        root, threads, nice, scan_interval, item_delay,
    )

    try:
        while True:
            started = time.monotonic()
            try:
                stats = run_once(
                    root, threads=threads, item_delay=item_delay
                )
            except Exception:  # noqa: BLE001 - never exit the long-running loop
                log.exception(
                    "scan round failed; retrying in %.1fs", scan_interval
                )
            else:
                log.info(
                    "round complete images=%d processed=%d skipped=%d "
                    "failed=%d read_errors=%d orphans_cleaned=%d "
                    "temps_cleaned=%d elapsed=%.1fs",
                    stats["images"], stats["processed"], stats["skipped"],
                    stats["failed"], stats["read_errors"],
                    stats["orphans_cleaned"], stats["temps_cleaned"],
                    time.monotonic() - started,
                )
            time.sleep(scan_interval)
    except KeyboardInterrupt:
        log.info("tag worker stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
