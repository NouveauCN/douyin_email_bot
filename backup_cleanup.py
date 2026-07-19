"""Periodic cleanup for media originals retained after edge cropping."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


logger = logging.getLogger("BackupCleanup")

_BACKUP_GLOB = "*_original.bak"
_SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class CleanupResult:
    """Summary of one backup cleanup scan."""

    scanned: int = 0
    deleted: int = 0
    retained: int = 0
    failed: int = 0


def _retention_timestamp(path: Path) -> float:
    """Return the safest available approximation of backup creation time.

    A rename preserves the source mtime, so an old download can have an old
    mtime immediately after becoming a backup.  ctime changes on rename on the
    deployment filesystem; taking the newer value protects those existing
    backups while newly-created backups also receive a fresh mtime.
    """
    stat = path.stat()
    return max(stat.st_mtime, stat.st_ctime)


def cleanup_expired_backups(
    root: Path,
    *,
    retention_days: int,
    now: float | None = None,
) -> CleanupResult:
    """Delete exact media backup files older than the retention window."""
    root = Path(root)
    if retention_days < 1:
        logger.error("Backup cleanup disabled: retention_days must be positive")
        return CleanupResult(failed=1)
    if not root.is_dir():
        logger.warning("Backup cleanup skipped: download directory missing: %s", root)
        return CleanupResult()

    cutoff = (time.time() if now is None else now) - retention_days * _SECONDS_PER_DAY
    scanned = deleted = retained = failed = 0

    try:
        candidates = root.rglob(_BACKUP_GLOB)
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                scanned += 1
                if _retention_timestamp(path) > cutoff:
                    retained += 1
                    continue
                path.unlink()
                deleted += 1
                logger.info("Deleted expired media backup: %s", path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failed += 1
                logger.warning("Failed to delete media backup %s: %s", path, exc)
    except OSError as exc:
        logger.warning("Backup cleanup scan failed for %s: %s", root, exc)
        failed += 1

    result = CleanupResult(scanned, deleted, retained, failed)
    logger.info(
        "Backup cleanup complete — scanned=%d deleted=%d retained=%d failed=%d",
        result.scanned,
        result.deleted,
        result.retained,
        result.failed,
    )
    return result


class BackupCleanupScheduler:
    """Run cleanup immediately at startup and then at a fixed interval."""

    def __init__(
        self,
        root: Path,
        *,
        retention_days: int,
        check_interval_days: int,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.retention_days = retention_days
        self.check_interval_seconds = max(1, check_interval_days) * _SECONDS_PER_DAY
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._next_check: float | None = None

    def run_if_due(self) -> CleanupResult | None:
        """Run one scan when due; return None between scheduled checks."""
        current = self._monotonic()
        if self._next_check is not None and current < self._next_check:
            return None
        self._next_check = current + self.check_interval_seconds
        return cleanup_expired_backups(
            self.root,
            retention_days=self.retention_days,
            now=self._wall_clock(),
        )
