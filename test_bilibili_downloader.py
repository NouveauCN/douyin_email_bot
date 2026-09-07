from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

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
        patch("bilibili_downloader._fetch_bilibili_metadata", return_value=("Bilibili", "20240101_010203")),
        patch("bilibili_downloader._process_downloaded_media") as process,
    ):
        result = downloader.download("https://www.bilibili.com/video/BV1test")

    assert result["success"] is True
    process.assert_called_once_with([video, cover], shared_root)


def test_extracts_bv_and_av_ids():
    from bilibili_downloader import _extract_video_id

    assert _extract_video_id("https://www.bilibili.com/video/BV1abC") == "BV1abC"
    assert _extract_video_id("https://www.bilibili.com/video/av12345") == "av12345"
    assert _extract_video_id("https://www.bilibili.com/?bvid=BV1abC") == "BV1abC"


def test_resolves_b23_short_link_to_bv():
    from bilibili_downloader import _extract_video_id

    response = Mock(status_code=302, headers={"location": "https://www.bilibili.com/video/BV1short"})
    with patch("bilibili_downloader.httpx.get", return_value=response):
        assert _extract_video_id("https://b23.tv/abc123") == "BV1short"


def test_rejects_untrusted_b23_redirect():
    from bilibili_downloader import _extract_video_id

    response = Mock(status_code=302, headers={"location": "https://evil.example/video/BV1bad"})
    with patch("bilibili_downloader.httpx.get", return_value=response):
        assert _extract_video_id("https://b23.tv/abc123") is None


def test_metadata_bad_payload_and_network_errors_fall_back():
    from bilibili_downloader import _fetch_bilibili_metadata

    with patch("bilibili_downloader.httpx.get", side_effect=ValueError("bad json")):
        assert _fetch_bilibili_metadata("BV1bad") == ("Bilibili", None)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": {"owner": {"name": object()}}}
    with patch("bilibili_downloader.httpx.get", return_value=response):
        assert _fetch_bilibili_metadata("BV1bad") == ("Bilibili", None)


def test_download_timestamp_uses_start_time_not_pubdate():
    from bilibili_downloader import _download_timestamp

    # Public metadata's publication time is intentionally not part of naming.
    assert _download_timestamp(1704067200) == datetime.fromtimestamp(1704067200).strftime("%Y%m%d_%H%M%S")


def test_renames_video_with_metadata_and_sanitizes_author(tmp_path):
    from bilibili_downloader import _rename_downloaded_files

    source = tmp_path / "yutto-name.mp4"
    source.write_bytes(b"video")
    result = _rename_downloaded_files(
        [source], tmp_path, "BV1abc", 'a/b:*"c', "20240101_010203"
    )

    assert result == [tmp_path / "a_b___c" / "20240101_010203_BV1abc.mp4"]
    assert result[0].read_bytes() == b"video"
    assert not source.exists()


def test_multi_part_and_conflicting_names_are_unique(tmp_path):
    from bilibili_downloader import _rename_downloaded_files

    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    result = _rename_downloaded_files(
        [first, second], tmp_path, "av123", "author", "20240101_010203"
    )
    assert [path.name for path in result] == [
        "20240101_010203_av123_P01.mp4",
        "20240101_010203_av123_P02.mp4",
    ]

    third = tmp_path / "third.mp4"
    third.write_bytes(b"3")
    base = tmp_path / "author" / "20240101_010203_av123.mp4"
    base.write_bytes(b"existing")
    conflict = _rename_downloaded_files(
        [third], tmp_path, "av123", "author", "20240101_010203"
    )
    assert conflict[0].name == "20240101_010203_av123_2.mp4"
