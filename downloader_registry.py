"""Compatibility import location for the public downloader registry."""

from download_task_service import Downloader, DownloaderRegistry, DownloadExecutor

__all__ = ["Downloader", "DownloaderRegistry", "DownloadExecutor"]
