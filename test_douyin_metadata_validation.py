"""Exercise the metadata boundary without contacting Douyin or downloading media."""

import asyncio
import io
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from f2_bootstrap import bootstrap_f2

bootstrap_f2()

import douyin_downloader as downloader
from f2.apps.douyin.model import PostDetail
from f2.apps.douyin.utils import TokenManager


URL = "https://www.douyin.com/video/123"
COOKIE = "sessionid=test-session; msToken=test-token"
UA = "Mozilla/5.0 Firefox/140.0"


def test_metadata_uses_captured_identity_and_restores_context_without_side_effects():
    before = downloader.identity_snapshot()
    seen = {}

    def factory(options):
        seen.update(options)
        handler = SimpleNamespace(enable_bark=True)

        async def fetch(aweme_id):
            assert handler.enable_bark is False
            request = PostDetail(aweme_id=aweme_id)
            assert request.msToken == "test-token"
            assert downloader._CURRENT_USER_AGENT.get() == UA
            return SimpleNamespace(_to_dict=lambda: {
                "aweme_id": aweme_id, "video_play_addr": ["https://media.invalid/video"],
            })

        handler.fetch_one_video = fetch
        return handler

    with patch.object(downloader, "DouyinHandler", side_effect=factory), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value="123")
    ), patch.object(downloader.DouyinDownloader, "download", side_effect=AssertionError):
        result = asyncio.run(downloader._validate_douyin_metadata_bound(URL, COOKIE, UA, 1))

    assert result["success"] is True
    assert seen["cookie"] == COOKIE
    assert seen["headers"]["User-Agent"] == UA
    assert downloader.identity_snapshot() == before
    assert downloader._CURRENT_MS_TOKEN.get() is None
    assert downloader._CURRENT_USER_AGENT.get() is None
    assert downloader._VALIDATION_DEADLINE.get() is None


def test_strict_validation_rejects_bootstrap_synthetic_fallback():
    request_started = []

    async def fetch(aweme_id):
        PostDetail(aweme_id=aweme_id)
        request_started.append(True)

    handler = SimpleNamespace(fetch_one_video=fetch)
    with patch.object(downloader, "DouyinHandler", return_value=handler), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value="123")
    ), patch.object(
        TokenManager, "_douyin_email_bot_original_gen_real_msToken",
        side_effect=RuntimeError("sensitive provider error"),
    ), patch.object(TokenManager, "gen_real_msToken", return_value="synthetic" * 16):
        result = downloader.validate_douyin_metadata(URL, "sessionid=test", UA)

    assert result["category"] == "token_failure"
    assert not request_started
    assert "sensitive" not in str(result)


def test_binding_failure_is_closed_before_resolving_or_fetching():
    with patch.object(downloader, "_configure_f2_request_identity", return_value=None), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock()
    ) as resolve:
        result = downloader.validate_douyin_metadata(URL, COOKIE, UA)
    assert result["category"] == "token_failure"
    resolve.assert_not_called()


def test_provider_logging_is_suppressed_only_in_validation_context():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    provider = logging.getLogger("f2")
    old_level = provider.level
    provider.setLevel(logging.DEBUG)
    provider.addHandler(handler)
    try:
        with downloader._silence_f2_validation_logs():
            provider.debug("https://example.invalid?msToken=secret-token")
            other = threading.Thread(target=lambda: provider.info("ordinary-download"))
            other.start()
            other.join()
        provider.info("after-validation")
    finally:
        provider.removeHandler(handler)
        provider.setLevel(old_level)
    assert "secret-token" not in stream.getvalue()
    assert "ordinary-download" in stream.getvalue()
    assert "after-validation" in stream.getvalue()


def test_late_token_generation_cannot_start_metadata_request():
    request_started = []

    def slow_generator(cls):
        time.sleep(0.03)
        return "valid-token"

    async def fetch(aweme_id):
        PostDetail(aweme_id=aweme_id)
        request_started.append(True)

    with patch.object(downloader, "DouyinHandler", return_value=SimpleNamespace(fetch_one_video=fetch)), patch.object(
        downloader, "_resolve_aweme_id", new=AsyncMock(return_value="123")
    ), patch.object(TokenManager, "_douyin_email_bot_original_gen_real_msToken", slow_generator):
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(downloader._validate_douyin_metadata_bound(URL, "sessionid=test", UA, 0.01))
    assert not request_started
    assert downloader._VALIDATION_DEADLINE.get() is None


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
