"""Focused tests for Douyin streaming downloads and slideshow accounting."""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

# F2 reads browser-model defaults during import, matching main.py's bootstrap.
from f2_bootstrap import bootstrap_f2

bootstrap_f2()

import douyin_downloader


class DouyinDownloadTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def _mock_client(self, handler):
        """Use httpx.MockTransport while replacing the downloader's client factory."""
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        with patch.object(douyin_downloader.httpx, "AsyncClient", side_effect=factory):
            yield

    async def test_stream_replaces_target_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            target.write_bytes(b"old")
            target.chmod(0o640)

            def handler(request):
                return httpx.Response(200, content=b"new content", request=request)

            downloader = douyin_downloader.DouyinDownloader(SimpleNamespace())
            with self._mock_client(handler):
                await downloader._download_file(
                    "https://example.test/video", target, {"max_retries": 1}
                )

            self.assertEqual(target.read_bytes(), b"new content")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(Path(temp_dir).glob(".video.mp4.*.tmp")), [])


    async def test_valid_https_redirect_is_cached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "short-links.json"
            location = "https://www.douyin.com/video/1234567890/?region=cn"
            with patch.object(douyin_downloader, "SHORT_LINK_CACHE_PATH", cache), patch(
                "douyin_downloader._resolve_short_link",
                new=AsyncMock(return_value=location),
            ):
                result = await douyin_downloader._resolve_aweme_id(
                    "https://v.douyin.com/AbC123/"
                )

            self.assertEqual(result, "1234567890")
            self.assertIn('"aweme_id": "1234567890"', cache.read_text())

    async def test_untrusted_redirect_is_rejected_without_cache_write(self):
        locations = (
            "http://www.douyin.com/video/1234567890",
            "https://evil.example/video/1234567890",
            "https://www.douyin.com/video/not-a-number",
        )
        for location in locations:
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temp_dir:
                cache = Path(temp_dir) / "short-links.json"
                with patch.object(douyin_downloader, "SHORT_LINK_CACHE_PATH", cache), patch(
                    "douyin_downloader._resolve_short_link",
                    new=AsyncMock(return_value=location),
                ):
                    with self.assertRaises(douyin_downloader.APITimeoutError):
                        await douyin_downloader._resolve_aweme_id(
                            "https://v.douyin.com/AbC123/"
                        )

                self.assertFalse(cache.exists())

    async def test_short_link_transport_uses_https_only(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "https://www.douyin.com/video/1234567890"},
                request=request,
            )

        with self._mock_client(handler):
            location = await douyin_downloader._resolve_short_link("AbC123", {})

        self.assertEqual(location, "https://www.douyin.com/video/1234567890")
        self.assertEqual([request.url.scheme for request in requests], ["https"])

    async def test_new_target_uses_readable_media_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"

            def handler(request):
                return httpx.Response(200, content=b"content", request=request)

            downloader = douyin_downloader.DouyinDownloader(SimpleNamespace())
            with self._mock_client(handler):
                await downloader._download_file(
                    "https://example.test/video", target, {"max_retries": 1}
                )

            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    async def test_failed_retries_keep_old_target_and_clean_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            target.write_bytes(b"old")
            calls = 0

            def handler(request):
                nonlocal calls
                calls += 1
                raise httpx.ReadError("connection lost", request=request)

            downloader = douyin_downloader.DouyinDownloader(SimpleNamespace())
            with self._mock_client(handler), patch(
                "douyin_downloader.asyncio.sleep", new=AsyncMock()
            ):
                with self.assertRaises(httpx.ReadError):
                    await downloader._download_file(
                        "https://example.test/video", target, {"max_retries": 2}
                    )

            self.assertEqual(calls, 2)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(Path(temp_dir).glob(".video.mp4.*.tmp")), [])

    async def test_empty_response_is_failure_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "empty.mp4"

            def handler(request):
                return httpx.Response(200, content=b"", request=request)

            downloader = douyin_downloader.DouyinDownloader(SimpleNamespace())
            with self._mock_client(handler):
                with self.assertRaisesRegex(ValueError, "empty"):
                    await downloader._download_file(
                        "https://example.test/empty", target, {"max_retries": 1}
                    )

            self.assertFalse(target.exists())
            self.assertEqual(list(Path(temp_dir).glob(".empty.mp4.*.tmp")), [])

    async def test_slideshow_all_failures_are_not_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def handler(request):
                raise httpx.ReadError("offline", request=request)

            downloader = douyin_downloader.DouyinDownloader(
                SimpleNamespace(folderize=False)
            )
            with self._mock_client(handler), patch(
                "douyin_downloader._process_downloaded_media", new=AsyncMock()
            ):
                result = await downloader._download_slideshow(
                    ["https://example.test/one.jpg", "https://example.test/two.jpg"],
                    [],
                    {"desc": "post"},
                    "123",
                    Path(temp_dir),
                    {"max_retries": 1},
                )

            self.assertFalse(result["success"])
            self.assertIsNone(result["filepath"])
            self.assertIn("失败 2 个", result["error"])
            self.assertIn("post", result["title"])
            self.assertEqual(list(Path(temp_dir).rglob("*.tmp")), [])

    async def test_slideshow_partial_success_keeps_stats_and_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def handler(request):
                if request.url.path.endswith("one.jpg"):
                    return httpx.Response(200, content=b"image", request=request)
                raise httpx.ReadError("offline", request=request)

            downloader = douyin_downloader.DouyinDownloader(
                SimpleNamespace(folderize=False)
            )
            with self._mock_client(handler), patch(
                "douyin_downloader._process_downloaded_media", new=AsyncMock()
            ), patch("douyin_downloader.asyncio.sleep", new=AsyncMock()):
                result = await downloader._download_slideshow(
                    ["https://example.test/one.jpg", "https://example.test/two.jpg"],
                    [],
                    {"desc": "post"},
                    "123",
                    Path(temp_dir),
                    {"max_retries": 1},
                )

            self.assertTrue(result["success"])
            self.assertTrue(result["partial"])
            self.assertEqual(result["filepath"], str(Path(temp_dir) / "slides"))
            self.assertIn("2图", result["title"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(
                len(list((Path(temp_dir) / "slides").glob("*.jpg"))), 1
            )

    async def test_zero_byte_existing_slideshow_file_is_downloaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def handler(request):
                return httpx.Response(200, content=b"replacement", request=request)

            downloader = douyin_downloader.DouyinDownloader(
                SimpleNamespace(folderize=False)
            )
            with self._mock_client(handler), patch(
                "douyin_downloader._download_timestamp", return_value="20260823_010203"
            ), patch("douyin_downloader._process_downloaded_media", new=AsyncMock()):
                old_target = root / "slides" / "20260823_010203_123_01.jpg"
                old_target.parent.mkdir(parents=True)
                old_target.write_bytes(b"")
                result = await downloader._download_slideshow(
                    ["https://example.test/one.jpg"],
                    [],
                    {"desc": "post"},
                    "123",
                    root,
                    {"max_retries": 1},
                )

            self.assertTrue(result["success"])
            self.assertEqual(old_target.read_bytes(), b"replacement")

    async def test_static_success_and_animated_failure_reports_static_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def handler(request):
                if request.url.path.endswith("one.jpg"):
                    return httpx.Response(200, content=b"image", request=request)
                raise httpx.ReadError("offline", request=request)

            downloader = douyin_downloader.DouyinDownloader(
                SimpleNamespace(folderize=True)
            )
            with self._mock_client(handler), patch(
                "douyin_downloader._process_downloaded_media", new=AsyncMock()
            ), patch("douyin_downloader.asyncio.sleep", new=AsyncMock()):
                result = await downloader._download_slideshow(
                    ["https://example.test/one.jpg"],
                    ["https://example.test/clip.mp4"],
                    {"desc": "post", "author": {"nickname": "author"}},
                    "123",
                    root,
                    {"max_retries": 1},
                )

            self.assertTrue(result["partial"])
            self.assertEqual(result["filepath"], str(root / "slides"))
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(result["failed_count"], 1)

    async def test_slideshow_discards_empty_url_entries(self):
        downloader = douyin_downloader.DouyinDownloader(
            SimpleNamespace(folderize=False)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await downloader._download_slideshow(
                ["", "  ", None],
                ["  "],
                {"desc": "post"},
                "123",
                Path(temp_dir),
                {"max_retries": 1},
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "图文内容为空，无法下载")

    async def test_retry_reuses_successful_items_when_timestamp_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls = {"one.jpg": 0, "two.jpg": 0}

            def handler(request):
                name = request.url.path.rsplit("/", 1)[-1]
                calls[name] += 1
                if name == "two.jpg" and calls[name] == 1:
                    raise httpx.ReadError("network timeout", request=request)
                return httpx.Response(200, content=name.encode(), request=request)

            downloader = douyin_downloader.DouyinDownloader(
                SimpleNamespace(folderize=False)
            )
            with self._mock_client(handler), patch(
                "douyin_downloader._process_downloaded_media", new=AsyncMock()
            ), patch("douyin_downloader.asyncio.sleep", new=AsyncMock()), patch(
                "douyin_downloader._download_timestamp",
                side_effect=["20260823_010203", "20260823_020304"],
            ):
                first = await downloader._download_slideshow(
                    ["https://example.test/one.jpg", "https://example.test/two.jpg"],
                    [], {"desc": "post"}, "123", root, {"max_retries": 1},
                )
                second = await downloader._download_slideshow(
                    ["https://example.test/one.jpg", "https://example.test/two.jpg"],
                    [], {"desc": "post"}, "123", root, {"max_retries": 1},
                )

            self.assertTrue(first["partial"])
            self.assertFalse(second["partial"])
            self.assertEqual(calls, {"one.jpg": 1, "two.jpg": 2})
            self.assertEqual(len(list((root / "slides").glob("*.jpg"))), 2)


if __name__ == "__main__":
    unittest.main()
