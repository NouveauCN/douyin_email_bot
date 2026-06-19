"""Douyin video downloader — wraps F2's async API behind a sync interface.

Fetches video metadata via F2, then downloads the video directly
using httpx to avoid F2's user-profile API (which often fails with
browser-exported cookies that lack httpOnly fields).
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx
from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import AwemeIdFetcher
from f2.exceptions import (
    APIConnectionError,
    APINotFoundError,
    APIResponseError,
    APITimeoutError,
)

logger = logging.getLogger("DouyinDownloader")


class DouyinDownloader:
    """Download Douyin videos using F2 metadata + direct httpx download.

    Bridges F2's async API into a synchronous call via asyncio.run().
    """

    def __init__(self, config):
        self.config = config

    def download(self, url: str) -> dict:
        """Download a single Douyin video from a share link.

        Returns a dict with keys:
            success: bool
            filepath: str | None  — local path to the downloaded .mp4
            title: str | None     — video description / author name
            error: str | None     — human-readable error (Chinese)
        """
        if not self.config.cookie:
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "未配置 Douyin cookie，请在 .env 中设置 DOUYIN_COOKIE",
            }

        download_dir = Path(self.config.download_path)
        download_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "url": url,
            "cookie": self.config.cookie,
            "timeout": self.config.timeout,
            "max_retries": self.config.max_retries,
            "proxies": {},
            "headers": {},
        }

        try:
            return asyncio.run(self._download_async(kwargs, download_dir))
        except APINotFoundError:
            logger.warning("Video not found: %s", url)
            return self._error("无效的抖音链接，未找到对应视频")
        except APIResponseError:
            logger.warning("Douyin API returned empty/invalid data for: %s", url)
            return self._error("视频不存在或已被删除")
        except APITimeoutError:
            logger.warning("Douyin request timed out: %s", url)
            return self._error("抖音服务器响应超时，请稍后重试")
        except APIConnectionError:
            logger.warning("Network error connecting to Douyin: %s", url)
            return self._error("网络连接失败，请检查网络后重试")
        except Exception:
            logger.exception("Unexpected error downloading: %s", url)
            return self._error("下载过程中发生未知错误")

    async def _download_async(self, kwargs: dict, download_dir: Path) -> dict:
        """Fetch metadata via F2, then download directly via httpx."""
        handler = DouyinHandler(kwargs | {"mode": "one", "path": str(download_dir),
                                          "naming": self.config.naming,
                                          "folderize": self.config.folderize,
                                          "max_tasks": 1,
                                          "music": False, "cover": False, "desc": False})

        # Step 1: Resolve short link → aweme_id
        aweme_id = await AwemeIdFetcher.get_aweme_id(kwargs["url"])
        logger.info("Resolved aweme_id: %s", aweme_id)

        # Step 2: Fetch video metadata (works with document.cookie)
        video_data = await handler.fetch_one_video(aweme_id)
        data = video_data._to_dict()

        # Step 3: Get the best available video URL
        play_urls = data.get("video_play_addr", [])
        if not play_urls:
            return self._error("视频链接已被作者删除或设为私密")

        video_url = play_urls[0]

        # Step 4: Build output filename
        title = data.get("desc") or data.get("nickname") or "Douyin Video"
        safe_title = _sanitize_filename(title)[:80]

        create_time = data.get("create_time", "")
        if create_time:
            try:
                date_str = datetime.strptime(str(create_time)[:10], "%Y-%m-%d").strftime("%Y%m%d")
            except ValueError:
                date_str = "unknown"
        else:
            date_str = "unknown"

        if self.config.folderize and data.get("nickname"):
            author_dir = _sanitize_filename(data["nickname"])[:50]
            save_dir = download_dir / author_dir
        else:
            save_dir = download_dir

        save_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{date_str}_{aweme_id}.mp4"
        filepath = save_dir / filename

        # Step 5: Download
        if filepath.exists():
            logger.info("File already exists: %s", filepath)
        else:
            await self._download_file(video_url, filepath, kwargs)

        logger.info("Downloaded: %s (%d bytes)", filepath, filepath.stat().st_size)

        return {
            "success": True,
            "filepath": str(filepath),
            "title": title,
            "error": None,
        }

    async def _download_file(self, url: str, filepath: Path, kwargs: dict) -> None:
        """Download a file from url to filepath with retries."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        }

        max_retries = kwargs.get("max_retries", 3)
        timeout = kwargs.get("timeout", 30)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, headers=headers
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    filepath.write_bytes(response.content)
                    return
            except httpx.HTTPError as e:
                last_error = e
                logger.warning("Download attempt %d/%d failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    await asyncio.sleep(1)

        raise last_error  # type: ignore[misc]

    @staticmethod
    def _error(msg: str) -> dict:
        return {"success": False, "filepath": None, "title": None, "error": msg}


def _sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in file names."""
    unsafe = r'<>:"/\|?*'
    for ch in unsafe:
        name = name.replace(ch, "_")
    return name.strip()
