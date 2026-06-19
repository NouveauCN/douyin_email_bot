"""Douyin video downloader — wraps F2's async API behind a sync interface."""

import asyncio
import logging
from pathlib import Path

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
    """Download Douyin videos using the F2 library.

    Bridges F2's async API into a synchronous call via asyncio.run(),
    suitable for use inside WeChatFerry's synchronous message loop.
    """

    def __init__(self, config):
        """
        Args:
            config: DouyinConfig dataclass instance.
        """
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
                "error": "未配置 Douyin cookie，请在 config.yaml 的 douyin.cookie 中设置",
            }

        download_dir = Path(self.config.download_path)
        download_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "url": url,
            "mode": "one",
            "path": str(download_dir),
            "cookie": self.config.cookie,
            "naming": self.config.naming,
            "folderize": self.config.folderize,
            "timeout": self.config.timeout,
            "max_retries": self.config.max_retries,
            "max_tasks": self.config.max_tasks,
            "music": False,
            "cover": False,
            "desc": False,
            "proxies": None,
        }

        try:
            return asyncio.run(self._download_async(kwargs))
        except APINotFoundError:
            logger.warning("Video not found: %s", url)
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "无效的抖音链接，未找到对应视频",
            }
        except APIResponseError:
            logger.warning("Douyin API returned empty/invalid data for: %s", url)
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "视频不存在或已被删除",
            }
        except APITimeoutError:
            logger.warning("Douyin request timed out: %s", url)
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "抖音服务器响应超时，请稍后重试",
            }
        except APIConnectionError:
            logger.warning("Network error connecting to Douyin: %s", url)
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "网络连接失败，请检查网络后重试",
            }
        except Exception:
            logger.exception("Unexpected error downloading: %s", url)
            return {
                "success": False,
                "filepath": None,
                "title": None,
                "error": "下载过程中发生未知错误",
            }

    async def _download_async(self, kwargs: dict) -> dict:
        """Core async download using F2 DouyinHandler."""
        handler = DouyinHandler(kwargs)

        # Resolve short link to aweme_id and fetch metadata
        aweme_id = await AwemeIdFetcher.get_aweme_id(kwargs["url"])
        video_data = await handler.fetch_one_video(aweme_id)

        # Perform the actual download
        await handler.handle_one_video()

        # Locate the downloaded file by matching aweme_id in the download directory
        download_root = Path(kwargs["path"])
        candidates = sorted(
            download_root.rglob(f"*{aweme_id}*.mp4"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        title = video_data.desc_raw or video_data.nickname_raw or "Douyin Video"

        return {
            "success": True,
            "filepath": str(candidates[0]) if candidates else None,
            "title": title,
            "error": None,
        }
