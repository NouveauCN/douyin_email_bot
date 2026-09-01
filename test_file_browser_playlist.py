"""Focused tests for the file-browser random video playlist contract."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self.expected_relpaths = []

        for author, filenames in {
            "alice": ("20260101_first.mp4", "20260102_second.mp4"),
            "bob": ("20260103_third.mp4", "20260104_fourth.mp4"),
        }.items():
            author_dir = self.download_dir / author
            author_dir.mkdir()
            for filename in filenames:
                (author_dir / filename).write_bytes(b"minimal mp4 fixture")
                self.expected_relpaths.append(f"{author}/{filename}")

        # These files ensure the route is collecting videos, not arbitrary files
        # or the separate slideshow directory.
        (self.download_dir / "alice" / "ignore.txt").write_text("ignore")
        (self.download_dir / "slides").mkdir()
        (self.download_dir / "slides" / "not-a-playlist-video.mp4").write_bytes(b"ignore")

        self.download_patch = patch.object(
            file_browser, "_DOWNLOAD_DIR", self.download_dir
        )
        self.download_patch.start()
        self.client = file_browser.app.test_client()

    def tearDown(self):
        self.download_patch.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _playlist_script(page):
        match = re.search(
            r"<script>\n(?P<script>// ── State ──.*?)(?=</script>)",
            page,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("playlist script was not rendered")
        return match.group("script")

    @staticmethod
    def _videos_from_page(page):
        match = re.search(
            r"const VIDEOS = (?P<videos>\[.*?\]);\nlet queue = \[\];",
            page,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("VIDEOS payload was not rendered")
        return json.loads(match.group("videos"))

    def test_playlist_payload_contains_each_author_video_once(self):
        response = self.client.get("/playlist")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        videos = self._videos_from_page(page)
        relpaths = [video["relpath"] for video in videos]

        self.assertEqual(relpaths, sorted(self.expected_relpaths))
        self.assertEqual(len(relpaths), 4)
        self.assertEqual(len(relpaths), len(set(relpaths)))
        self.assertNotIn("slides/not-a-playlist-video.mp4", relpaths)
        for relpath in relpaths:
            self.assertIn(relpath, page)

    def test_random_queue_is_built_from_all_distinct_video_indices(self):
        script = self._playlist_script(
            self.client.get("/playlist").get_data(as_text=True)
        )

        self.assertIn("queue = VIDEOS.map((v, i) => i);", script)
        self.assertIn("shuffleArray(queue);", script)
        self.assertIn("[arr[i], arr[j]] = [arr[j], arr[i]];", script)
        self.assertNotIn("shuffleArray(VIDEOS)", script)

    def test_initial_playback_uses_shared_queue_and_separate_indices(self):
        script = self._playlist_script(
            self.client.get("/playlist").get_data(as_text=True)
        )

        self.assertIn(
            "function currentVideoIndex() {\n  return queue[currentQueueIdx];\n}",
            script,
        )
        self.assertIn(
            "function playIndex(queueIdx, scroll) {\n  currentQueueIdx = queueIdx;",
            script,
        )
        self.assertIn(
            "function playVideo(videoIdx, scroll) {\n  const queueIdx = queue.indexOf(videoIdx);",
            script,
        )

        init = script[script.index("// ── Init ──") :]
        self.assertLess(init.index("buildQueue();"), init.index("if (queue.length > 0)"))
        self.assertIn("playIndex(0);", init)

    def test_video_end_advances_only_to_the_next_queue_position(self):
        script = self._playlist_script(
            self.client.get("/playlist").get_data(as_text=True)
        )
        match = re.search(
            r"player\.addEventListener\('ended', function\(\) \{(?P<body>.*?)\n\}\);",
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        ended_body = match.group("body")

        self.assertRegex(
            ended_body,
            r"if \(currentQueueIdx < queue\.length - 1\) \{\s*"
            r"playIndex\(currentQueueIdx \+ 1\);\s*\}",
        )
        self.assertNotIn("currentVideoIndex()", ended_body)
        self.assertNotIn("queue =", ended_body)
        self.assertNotIn("VIDEOS", ended_body)


if __name__ == "__main__":
    unittest.main()
