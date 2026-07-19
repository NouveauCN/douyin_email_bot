import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backup_cleanup import BackupCleanupScheduler, CleanupResult, cleanup_expired_backups


DAY = 24 * 60 * 60


class BackupCleanupTests(unittest.TestCase):
    def test_cleanup_deletes_only_expired_original_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            expired = root / "old_original.bak"
            recent = root / "new_original.bak"
            unrelated = root / "old.bak"
            for path in (expired, recent, unrelated):
                path.write_bytes(b"media")

            now = 2_000_000_000.0
            os.utime(expired, (now - 29 * DAY, now - 29 * DAY))
            os.utime(recent, (now - 27 * DAY, now - 27 * DAY))

            def timestamp(path: Path) -> float:
                return now - (29 if path == expired else 27) * DAY

            with patch("backup_cleanup._retention_timestamp", side_effect=timestamp):
                result = cleanup_expired_backups(root, retention_days=28, now=now)

            self.assertEqual(result, CleanupResult(scanned=2, deleted=1, retained=1))
            self.assertFalse(expired.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_scheduler_runs_at_startup_and_then_weekly(self):
        monotonic_now = [100.0]
        scheduler = BackupCleanupScheduler(
            Path("/downloads"),
            retention_days=28,
            check_interval_days=7,
            monotonic=lambda: monotonic_now[0],
            wall_clock=lambda: 2_000_000_000.0,
        )

        with patch(
            "backup_cleanup.cleanup_expired_backups",
            return_value=CleanupResult(),
        ) as cleanup:
            self.assertIsNotNone(scheduler.run_if_due())
            self.assertIsNone(scheduler.run_if_due())
            monotonic_now[0] += 7 * DAY
            self.assertIsNotNone(scheduler.run_if_due())

        self.assertEqual(cleanup.call_count, 2)


if __name__ == "__main__":
    unittest.main()
