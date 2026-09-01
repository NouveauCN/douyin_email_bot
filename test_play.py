"""Focused tests for the standalone player queue and dispatch logic."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import play


class BuildRandomQueueTests(unittest.TestCase):
    def test_seed_is_reproducible_and_preserves_input_as_a_permutation(self):
        videos = [Path(f"video-{index}.mp4") for index in range(8)]
        original = videos.copy()

        first = play.build_random_queue(videos, seed=12345)
        second = play.build_random_queue(videos, seed=12345)

        self.assertEqual(first, second)
        self.assertEqual(len(first), len(videos))
        self.assertCountEqual(first, videos)
        self.assertEqual(videos, original)

    def test_repeated_paths_and_symlinks_to_one_target_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.mp4"
            other = root / "other.mp4"
            alias = root / "alias.mp4"
            target.touch()
            other.touch()
            try:
                alias.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            videos = [target, target, alias, other, alias]
            original = videos.copy()
            queue = play.build_random_queue(videos, seed=7)

            self.assertEqual(len(queue), 2)
            self.assertCountEqual(queue, [target, other])
            self.assertEqual(videos, original)


class PlayAllDispatchTests(unittest.TestCase):
    def test_playlist_branch_receives_built_queue(self):
        built_queue = [Path("queued.mp4")]

        with (
            patch.object(play, "build_random_queue", return_value=built_queue) as build,
            patch.object(play, "_detect_player", return_value=("mpv", "playlist")),
            patch.object(play, "_play_with_playlist") as play_with_playlist,
            patch.object(play, "_play_sequential") as play_sequential,
        ):
            play.play_all(
                [Path("input.mp4")],
                player_cmd="mpv",
                resolution="1024x576",
            )

        build.assert_called_once_with([Path("input.mp4")])
        play_with_playlist.assert_called_once_with(built_queue, "mpv", "1024x576")
        play_sequential.assert_not_called()

    def test_sequential_branch_receives_built_queue(self):
        built_queue = [Path("queued.mp4")]

        with (
            patch.object(play, "build_random_queue", return_value=built_queue) as build,
            patch.object(play, "_detect_player", return_value=("custom-player", "explicit")),
            patch.object(play, "_play_with_playlist") as play_with_playlist,
            patch.object(play, "_play_sequential") as play_sequential,
        ):
            play.play_all(
                [Path("input.mp4")],
                player_cmd="custom-player",
                preload_count=5,
                resolution="800x600",
            )

        build.assert_called_once_with([Path("input.mp4")])
        play_sequential.assert_called_once_with(
            built_queue, "custom-player", 5, "800x600"
        )
        play_with_playlist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
