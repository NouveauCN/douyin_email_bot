import multiprocessing
import tempfile
import unittest
from pathlib import Path

from media_file_lock import MediaFileLockBusy, lock_path, media_file_lock


def _try_lock(root: str, queue) -> None:
    target = Path(root) / "author" / "clip.mp4"
    try:
        with media_file_lock(target, root=Path(root), timeout=0.2):
            queue.put("acquired")
    except MediaFileLockBusy:
        queue.put("busy")


class MediaFileLockTests(unittest.TestCase):
    def test_lock_is_hashed_and_reentrant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "author" / "clip.mp4"
            target.parent.mkdir()
            expected = root / ".media_locks"
            with media_file_lock(target, root=root):
                with media_file_lock(root / "author" / "../author/clip.mp4", root=root):
                    self.assertTrue(lock_path(target, root=root).parent == expected)
                    self.assertEqual(len(list(expected.glob("*.lock"))), 1)

    def test_lock_blocks_another_process_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "author" / "clip.mp4"
            target.parent.mkdir()
            queue = multiprocessing.Queue()
            with media_file_lock(target, root=root):
                process = multiprocessing.Process(target=_try_lock, args=(temp_dir, queue))
                process.start()
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(queue.get(timeout=1), "busy")

            process = multiprocessing.Process(target=_try_lock, args=(temp_dir, queue))
            process.start()
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(queue.get(timeout=1), "acquired")

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "clip.mp4"
            with self.assertRaises(RuntimeError):
                with media_file_lock(target, root=root):
                    raise RuntimeError("failure")
            with media_file_lock(target, root=root, timeout=0.1):
                self.assertTrue(lock_path(target, root=root).exists())


if __name__ == "__main__":
    unittest.main()
