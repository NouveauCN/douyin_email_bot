import multiprocessing
import tempfile
import unittest
from pathlib import Path

import pytest

from media_file_lock import MediaFileLockBusy, lock_path, media_file_lock


pytestmark = pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded.*:DeprecationWarning"
)


def _try_lock(root: str, queue) -> None:
    _try_specific_lock(root, "author/clip.mp4", queue)


def _try_specific_lock(root: str, relative: str, queue) -> None:
    target = Path(root) / relative
    try:
        with media_file_lock(target, root=Path(root), timeout=0.2):
            queue.put("acquired")
    except MediaFileLockBusy:
        queue.put("busy")


def _wait_then_lock(root: str, ready, proceed, queue) -> None:
    ready.set()
    proceed.wait(timeout=5)
    _try_lock(root, queue)


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
                    # One shared-tree sidecar plus one canonical target sidecar.
                    self.assertEqual(len(list(expected.glob("*.lock"))), 2)

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

    def test_directory_delete_lock_conflicts_with_child_file_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child = root / "author" / "clip.mp4"
            child.parent.mkdir()
            queue = multiprocessing.Queue()
            with media_file_lock(child, root=root):
                process = multiprocessing.Process(
                    target=_try_specific_lock,
                    args=(temp_dir, "author", queue),
                )
                process.start()
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(queue.get(timeout=1), "busy")

    def test_forked_child_does_not_retain_parent_lock_descriptor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "author" / "clip.mp4"
            target.parent.mkdir()
            queue = multiprocessing.Queue()
            ready = multiprocessing.Event()
            proceed = multiprocessing.Event()
            held = media_file_lock(target, root=root)
            held.acquire()
            process = multiprocessing.Process(
                target=_wait_then_lock,
                args=(temp_dir, ready, proceed, queue),
            )
            process.start()
            self.assertTrue(ready.wait(timeout=5))
            held.release()
            proceed.set()
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
