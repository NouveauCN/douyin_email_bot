"""Douyin video & slideshow downloader — wraps F2's async API behind a sync interface.

Fetches metadata via F2, then downloads the content directly using httpx.
Supports:
  - Regular videos (media_type=4)
  - Slideshows / 图文 (media_type=42, aweme_type=68)
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from colorama import Fore, Style
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

        # ── Quick cookie quality pre-check ──────────────────────────
        cookie_len = len(self.config.cookie)
        auth_indicators = ["sessionid", "passport_csrf_token", "odin_tt", "uid"]
        has_auth = any(k in self.config.cookie for k in auth_indicators)
        if cookie_len < 500 and not has_auth:
            logger.warning(
                "Cookie looks too short (%d chars) and lacks auth tokens — "
                "download will likely fail",
                cookie_len,
            )
        logger.debug(
            "Cookie: %d chars, has_auth_tokens=%s",
            cookie_len, has_auth,
        )

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
        logger.debug("Resolved aweme_id: %s", aweme_id)

        # Step 2: Fetch video metadata (works with document.cookie)
        video_data = await handler.fetch_one_video(aweme_id)
        data = video_data._to_dict()

        # Step 3: Decide media type — video, slideshow, or error
        play_urls = data.get("video_play_addr", [])
        images = data.get("images", [])
        media_type = data.get("media_type", -1)

        # ── Slideshow / 图文 ────────────────────────────────────────
        if not play_urls and images:
            images_video = data.get("images_video", [])
            return await self._download_slideshow(
                images, images_video, data, aweme_id, download_dir, kwargs,
            )

        # ── No playable content ────────────────────────────────────
        if not play_urls:
            # ── Diagnostic: log what the API did return ────────────
            api_status = data.get("api_status_code", "N/A")
            is_delete = data.get("is_delete", "N/A")
            is_prohibited = data.get("is_prohibited", "N/A")
            private_status = data.get("private_status", "N/A")

            logger.warning(
                "No video_play_addr or images for aweme_id=%s — "
                "api_status=%s, media_type=%s, is_delete=%s, "
                "is_prohibited=%s, private=%s, cookie_len=%d",
                aweme_id, api_status, media_type, is_delete,
                is_prohibited, private_status, len(kwargs.get("cookie", "")),
            )

            # Build a human-readable reason from the API flags
            if is_delete is True or is_delete == 1:
                return self._error("视频已被作者删除")
            if is_prohibited is True or is_prohibited == 1:
                return self._error("视频被平台屏蔽（违规或审核中）")
            if private_status is True or private_status == 1:
                return self._error("视频已设为私密，仅作者可见")
            if api_status not in (None, 0, "0") and api_status != "N/A":
                return self._error(f"抖音接口返回异常 (status={api_status})")

            # Generic fallback
            return self._error(
                "视频链接已被作者删除或设为私密"
            )

        # ── Regular video ──────────────────────────────────────────
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
            logger.info(f"{Fore.YELLOW}已存在: %s", filepath.name)
        else:
            await self._download_file(video_url, filepath, kwargs)
            logger.info(
                f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载完成: %s (%.1f MB)",
                filepath.name,
                filepath.stat().st_size / 1_000_000,
            )

        return {
            "success": True,
            "filepath": str(filepath),
            "title": title,
            "error": None,
        }

    async def _download_slideshow(
        self, images: list, images_video: list, data: dict, aweme_id: str,
        download_dir: Path, kwargs: dict,
    ) -> dict:
        """Download both static images AND animated video clips from a 图文 post.

        Douyin slideshows return:
          - images: static .webp images
          - images_video: short .mp4 clips (animated version shown in-app)
        """
        title = data.get("desc") or data.get("nickname") or "Douyin Slideshow"

        # Build save path
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
            base_dir = download_dir / author_dir
        else:
            base_dir = download_dir

        slide_dir_name = f"{date_str}_{aweme_id}_slides"
        slide_dir = base_dir / slide_dir_name
        slide_dir.mkdir(parents=True, exist_ok=True)

        # ── Build download queues ──────────────────────────────────
        # Static images
        static_urls = [u for u in images if isinstance(u, str)]
        static_ext = ".webp"
        if static_urls:
            first = static_urls[0]
            if ".jpg" in first or ".jpeg" in first:
                static_ext = ".jpg"
            elif ".png" in first:
                static_ext = ".png"

        # Animated video clips
        video_urls = [u for u in images_video if isinstance(u, str)]

        # Flatten into a single download list: (url, save_name, label)
        downloads: list[tuple[str, str, str]] = []
        for i, url in enumerate(static_urls):
            downloads.append((url, f"{i + 1:02d}{static_ext}", "图片"))
        for i, url in enumerate(video_urls):
            downloads.append((url, f"{i + 1:02d}.mp4", "动图"))

        if not downloads:
            return self._error("图文内容为空，无法下载")

        logger.info(
            "Downloading slideshow: %d 图片 + %d 动图 to %s",
            len(static_urls), len(video_urls), slide_dir,
        )

        done = 0
        total_size = 0
        for url, filename, label in downloads:
            filepath = slide_dir / filename

            if filepath.exists():
                logger.info(f"{Fore.YELLOW}已存在: %s/%s", slide_dir_name, filename)
                done += 1
                total_size += filepath.stat().st_size
                continue

            try:
                await self._download_file(url, filepath, kwargs)
                done += 1
                total_size += filepath.stat().st_size
            except Exception as exc:
                logger.warning("Failed to download %s %s: %s", label, filename, exc)

        logger.info(
            f"{Fore.GREEN}{Style.BRIGHT}[DONE] 图文下载完成: %s "
            f"(%d图片+%d动图, %.1f MB)",
            slide_dir_name, len(static_urls), len(video_urls),
            total_size / 1_000_000,
        )

        return {
            "success": True,
            "filepath": str(slide_dir),
            "title": f"{title} [图文 {len(static_urls)}图+{len(video_urls)}动图]",
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
