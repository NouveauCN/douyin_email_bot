from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from bilibili_downloader import BilibiliDownloader
from config_loader import BilibiliConfig


def test_download_uses_shared_media_root_for_videos_and_moved_covers(tmp_path):
    shared_root = tmp_path / "downloads"
    download_dir = shared_root / "bilibili"
    video = download_dir / "video.mp4"
    cover = shared_root / "slides" / "bilibili_cover.jpg"
    downloader = BilibiliDownloader(
        BilibiliConfig(download_path=str(download_dir), timeout=30)
    )

    with (
        patch(
            "bilibili_downloader.subprocess.run",
            return_value=CompletedProcess([], 0, stdout="Title: demo", stderr=""),
        ),
        patch("bilibili_downloader._move_cover_files", return_value=[cover]),
        patch("bilibili_downloader._collect_downloaded_files", return_value=[video]),
        patch("bilibili_downloader._process_downloaded_media") as process,
    ):
        result = downloader.download("https://www.bilibili.com/video/BV1test")

    assert result["success"] is True
    process.assert_called_once_with([video, cover], shared_root)
