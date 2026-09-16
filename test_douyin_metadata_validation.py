"""Exercise the metadata boundary without contacting Douyin or downloading media."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import douyin_downloader as downloader


URL = "https://www.douyin.com/video/123"
COOKIE = "sessionid=test-session; msToken=test-token"
UA = "Mozilla/5.0 Firefox/140.0"


def test_metadata_validation_succeeds_with_playwright_data():
    fake_video_data = SimpleNamespace(
        _to_dict=lambda: {
            "aweme_id": "123",
            "video_play_addr": ["https://media.invalid/video"],
            "desc": "test video",
            "nickname": "test_author",
        }
    )
    with patch.object(
        downloader, "_playwright_fetch_video_data",
        new=AsyncMock(return_value=fake_video_data),
    ), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value="123"),
    ):
        result = asyncio.run(
            downloader._validate_douyin_metadata_bound(URL, COOKIE, UA, 5)
        )

    assert result["success"] is True
    assert result["category"] == "success"
    assert result["aweme_id"] == "123"
    assert result["title"] == "test video"


def test_metadata_validation_rejects_no_playwright_data():
    with patch.object(
        downloader, "_playwright_fetch_video_data",
        new=AsyncMock(return_value=None),
    ), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value="123"),
    ):
        result = downloader.validate_douyin_metadata(URL, COOKIE, UA)

    assert result["success"] is False
    assert result["category"] == "unavailable"


def test_metadata_validation_rejects_empty_aweme_id():
    with patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value=""),
    ):
        result = downloader.validate_douyin_metadata(URL, COOKIE, UA)

    assert result["success"] is False
    assert result["category"] == "unavailable"


def test_metadata_validation_rejects_invalid_url():
    result = downloader.validate_douyin_metadata("not-a-url", COOKIE, UA)
    assert result["success"] is False
    assert result["category"] == "invalid_url"


def test_metadata_validation_rejects_empty_cookie():
    result = downloader.validate_douyin_metadata(URL, "", UA)
    assert result["success"] is False
    assert result["category"] == "token_failure"


def test_timeout_retains_worker_slot_until_cleanup():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)

    def blocked(*args):
        entered.set()
        release.wait(2)
        finished.set()
        return {"success": True}

    with patch.object(downloader, "_VALIDATION_SLOTS", slots), patch.object(
        downloader, "_validate_douyin_metadata_async", side_effect=blocked
    ):
        try:
            result = downloader.validate_douyin_metadata(URL, COOKIE, UA, timeout=0.1)
            assert entered.is_set()
            assert result["category"] == "timeout"
            assert not slots.acquire(blocking=False)
        finally:
            release.set()
            assert finished.wait(1)
            assert slots.acquire(timeout=1)
            slots.release()


def test_concurrent_validation_is_bounded():
    """Verify that more than 2 concurrent validations are rejected."""
    slots = downloader._VALIDATION_SLOTS

    # Exhaust all slots
    acquired = []
    for _ in range(2):
        assert slots.acquire(blocking=False)
        acquired.append(True)

    result = downloader.validate_douyin_metadata(URL, COOKIE, UA, timeout=0.1)
    assert result["category"] == "timeout"

    # Release slots
    for _ in acquired:
        slots.release()
