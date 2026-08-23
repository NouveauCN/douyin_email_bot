"""Regression tests for file-browser deletion safety and dedup cleanup."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self.download_dir.mkdir(exist_ok=True)
        self.download_patch = patch.object(
            file_browser, "_DOWNLOAD_DIR", self.download_dir
        )
        self.download_patch.start()
        self.client = file_browser.app.test_client()
        self.dedup_index = file_browser._DEDUP_INDEX
        self.pending_dups = file_browser._PENDING_DUPS
        file_browser._DEDUP_INDEX = {}
        file_browser._PENDING_DUPS = []
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def tearDown(self):
        file_browser._DEDUP_INDEX = self.dedup_index
        file_browser._PENDING_DUPS = self.pending_dups
        self.download_patch.stop()
        self.tempdir.cleanup()

    def test_normalized_root_paths_are_rejected(self):
        (self.download_dir / "author").mkdir()
        (self.download_dir / "slides").mkdir()
        marker = self.download_dir / "keep.txt"
        marker.write_text("keep")

        for path in (".", "./", "author/..", "slides/../"):
            with self.subTest(path=path):
                response = self.client.post("/api/delete", json={"path": path})
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.get_json()["success"])
                self.assertTrue(self.download_dir.is_dir())
                self.assertTrue(marker.exists())

    def test_directory_delete_removes_all_dedup_state_below_directory(self):
        author = self.download_dir / "author"
        author.mkdir()
        (author / "new.mp4").write_bytes(b"new")
        (author / "match.mp4").write_bytes(b"match")
        (self.download_dir / "keep.mp4").write_bytes(b"keep")

        file_browser._DEDUP_INDEX.update({
            "author/new.mp4": (1, b"new"),
            "author/match.mp4": (2, b"match"),
            "keep.mp4": (3, b"keep"),
        })
        file_browser._PENDING_DUPS.append({
            "new_file": "author/new.mp4",
            "match_file": "author/match.mp4",
            "dhash_dist": 1,
            "mse": 1.0,
            "similarity_pct": 99.0,
        })

        response = self.client.post("/api/delete", json={"path": "author"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(author.exists())
        self.assertNotIn("author/new.mp4", file_browser._DEDUP_INDEX)
        self.assertNotIn("author/match.mp4", file_browser._DEDUP_INDEX)
        self.assertEqual(file_browser._PENDING_DUPS, [])
        self.assertIn("keep.mp4", file_browser._DEDUP_INDEX)


if __name__ == "__main__":
    unittest.main()
