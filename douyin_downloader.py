"""Douyin video & slideshow downloader — wraps F2's async API behind a sync interface.

Fetches metadata via F2, then downloads the content directly using httpx.
Supports:
  - Regular videos (media_type=4)
  - Slideshows / 图文 (media_type=42, aweme_type=68)
"""

import asyncio
import json
import logging
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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

from media_processor import log_process_result, process_media
from media_file_lock import media_file_lock

logger = logging.getLogger("DouyinDownloader")

_FIREFOX_UA_PLATFORM = (
    "X11; Linux x86_64" if sys.platform.startswith("linux") else
    "Macintosh; Intel Mac OS X 10.15" if sys.platform == "darwin" else
    "Windows NT 10.0; Win64; x64"
)
_FIREFOX_USER_AGENT = (
    f"Mozilla/5.0 ({_FIREFOX_UA_PLATFORM}; rv:130.0) "
    "Gecko/20100101 Firefox/130.0"
)

DOUYIN_SHORT_RE = re.compile(r"^https://v\.douyin\.com/([A-Za-z0-9_-]+)/?$")
DOUYIN_SHORT_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DOUYIN_AWEME_ID_RE = re.compile(r"/(?:share/)?(?:video|note)/(\d+)")
DOUYIN_AWEME_PATH_RE = re.compile(r"^/(?:share/)?(?:video|note)/(\d+)/?$")
_DOUYIN_REDIRECT_HOSTS = frozenset({
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "www.iesdouyin.com",
})
SHORT_LINK_CACHE_PATH = Path(
    os.getenv("DOUYIN_SHORT_LINK_CACHE")
    or Path(__file__).parent / "logs" / "short_link_cache.json"
)
SHORT_LINK_CACHE_SCHEMA = "https-validated-v1"


def _short_link_verify() -> str | bool:
    """Return strict system verification or an explicitly configured CA path."""
    return os.getenv("DOUYIN_SHORT_LINK_CA_BUNDLE") or True


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
        auth_indicators = ["sessionid", "sessionid_ss", "sid_guard", "uid", "LOGIN_STATUS"]
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

        url = _normalize_share_url(url)
        download_dir = Path(self.config.download_path)
        download_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "url": url,
            "cookie": self.config.cookie,
            "timeout": self.config.timeout,
            "max_retries": self.config.max_retries,
            "proxies": {},
            "headers": {"User-Agent": _FIREFOX_USER_AGENT},
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
        aweme_id = await _resolve_aweme_id(kwargs["url"])
        logger.debug("Resolved aweme_id: %s", aweme_id)

        # Step 2: Fetch video metadata (works with document.cookie)
        video_data = await handler.fetch_one_video(aweme_id)
        data = video_data._to_dict()

        # Step 3: Select the best available video stream before deciding the
        # media type.  F2 exposes all bitrate entries in the raw response, but
        # its convenience property only points at the first entry.
        play_urls = data.get("video_play_addr", [])
        video_url = _select_best_video_url(video_data, play_urls)
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
        # Step 4: Build output filename
        title = data.get("desc") or data.get("nickname") or "Douyin Video"

        download_time = _download_timestamp()

        if self.config.folderize and data.get("nickname"):
            author_dir = _sanitize_filename(data["nickname"])[:50]
            save_dir = download_dir / author_dir
        else:
            save_dir = download_dir

        save_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{download_time}_{aweme_id}.mp4"
        filepath = save_dir / filename

        # Step 5: Download
        downloaded = False
        if _has_downloaded_content(filepath):
            logger.info(f"{Fore.YELLOW}已存在: %s", filepath.name)
        else:
            await self._download_file(video_url, filepath, kwargs)
            downloaded = True
            logger.info(
                f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载完成: %s (%.1f MB)",
                filepath.name,
                filepath.stat().st_size / 1_000_000,
            )

        if downloaded:
            await _process_downloaded_media(filepath, download_dir)

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
        """Download static images + animated video clips from a 图文 post.

        Static images (.webp/.jpg/.png) → downloads/slides/
        Animated clips (.mp4) → downloads/<author>/ (same as regular videos)
        """
        title = data.get("desc") or data.get("nickname") or "Douyin Slideshow"

        # Build save paths
        download_time = _download_timestamp()
        prefix = f"{download_time}_{aweme_id}"

        # Static images → slides/
        slides_dir = download_dir / "slides"
        slides_dir.mkdir(parents=True, exist_ok=True)

        # ── Build download queues ──────────────────────────────────
        static_urls = [u.strip() for u in images if isinstance(u, str) and u.strip()]
        static_ext = ".webp"
        if static_urls:
            first = static_urls[0]
            if ".jpg" in first or ".jpeg" in first:
                static_ext = ".jpg"
            elif ".png" in first:
                static_ext = ".png"

        video_urls = [u.strip() for u in images_video if isinstance(u, str) and u.strip()]

        # Animated clips → author folder (same logic as videos)
        # Only create the author directory if there are actually clips to put there.
        if video_urls and self.config.folderize and data.get("nickname"):
            author_dir = _sanitize_filename(data["nickname"])[:50]
            video_dir = download_dir / author_dir
        else:
            video_dir = download_dir
        if video_urls:
            video_dir.mkdir(parents=True, exist_ok=True)

        # (url, filepath, label)
        downloads: list[tuple[str, Path, str]] = []

        # Static images → slides/{prefix}_{NN}.ext
        for i, url in enumerate(static_urls):
            fname = f"{prefix}_{i + 1:02d}{static_ext}"
            existing = _find_existing_slideshow_file(
                slides_dir, f"_{aweme_id}_{i + 1:02d}{static_ext}"
            )
            downloads.append((url, existing or (slides_dir / fname), "图片"))

        # Animated clips → <author>/{prefix}.mp4 (or {prefix}_{NN}.mp4 if multiple)
        for i, url in enumerate(video_urls):
            if len(video_urls) == 1:
                fname = f"{prefix}.mp4"
                suffix = f"_{aweme_id}.mp4"
            else:
                fname = f"{prefix}_{i + 1:02d}.mp4"
                suffix = f"_{aweme_id}_{i + 1:02d}.mp4"
            existing = _find_existing_slideshow_file(video_dir, suffix)
            downloads.append((url, existing or (video_dir / fname), "动图"))

        if not downloads:
            return self._error("图文内容为空，无法下载")

        # Determine the human-readable target dir for logging/return
        if video_urls and video_dir != download_dir:
            target_label = video_dir.name
        elif video_urls:
            target_label = "downloads"
        else:
            target_label = "slides"

        logger.info(
            "Downloading slideshow: %d 图片 -> slides/, %d 动图 -> %s/",
            len(static_urls), len(video_urls), target_label,
        )

        done = 0
        total_size = 0
        successful_paths: list[Path] = []
        failures: list[str] = []
        for url, filepath, label in downloads:
            if _has_downloaded_content(filepath):
                logger.info(f"{Fore.YELLOW}已存在: %s", filepath)
                done += 1
                total_size += filepath.stat().st_size
                successful_paths.append(filepath)
                continue

            try:
                await self._download_file(url, filepath, kwargs)
                done += 1
                total_size += filepath.stat().st_size
                successful_paths.append(filepath)
                await _process_downloaded_media(filepath, download_dir)
            except Exception as exc:
                logger.warning("Failed to download %s %s: %s", label, filepath.name, exc)
                failures.append(f"{label} {filepath.name}: {exc}")

        logger.info(
            f"{Fore.GREEN}{Style.BRIGHT}[DONE] 图文下载完成: %s "
            f"(%d图片+%d动图, %.1f MB)",
            prefix, len(static_urls), len(video_urls),
            total_size / 1_000_000,
        )

        total = len(downloads)
        result_title = f"{title} [图文 {len(static_urls)}图+{len(video_urls)}动图]"
        partial = bool(done and failures)
        if successful_paths:
            successful_parents = {path.parent for path in successful_paths}
            result_path = successful_paths[0].parent if len(successful_parents) == 1 else download_dir
        else:
            result_path = None
        return {
            "success": done > 0,
            "partial": partial,
            "filepath": str(result_path) if result_path else None,
            "files": [str(path) for path in successful_paths],
            "file_count": done,
            "failed_count": len(failures),
            "failed_items": failures,
            "title": result_title,
            "error": None if done else (
                f"图文内容下载失败 {len(failures)} 个，未生成有效文件："
                + "；".join(failures)
            ),
        }

    async def _download_file(self, url: str, filepath: Path, kwargs: dict) -> None:
        """Stream a file into a same-directory temp file, then replace atomically."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        }

        max_retries = max(1, int(kwargs.get("max_retries", 3)))
        timeout = kwargs.get("timeout", 30)

        last_error = None
        for attempt in range(1, max_retries + 1):
            temp_path: Path | None = None
            temp_fd: int | None = None
            try:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                temp_fd, temp_name = tempfile.mkstemp(
                    prefix=f".{filepath.name}.",
                    suffix=".tmp",
                    dir=str(filepath.parent),
                )
                temp_path = Path(temp_name)
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, headers=headers,
                    trust_env=False,
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with os.fdopen(temp_fd, "wb") as output:
                            temp_fd = None
                            total_bytes = 0
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    output.write(chunk)
                                    total_bytes += len(chunk)
                            if total_bytes == 0:
                                raise ValueError("download response was empty")
                            output.flush()
                            os.fsync(output.fileno())

                if filepath.exists():
                    final_mode = stat.S_IMODE(filepath.stat().st_mode)
                else:
                    final_mode = 0o644
                temp_path.chmod(final_mode)
                # Only the atomic publish is locked; network streaming stays
                # outside the media critical section.
                with media_file_lock(
                    filepath,
                    root=Path(getattr(self.config, "download_path", filepath.parent)),
                    timeout=5,
                ):
                    temp_path.replace(filepath)
                temp_path = None
                return
            except Exception as e:
                last_error = e
                logger.warning("Download attempt %d/%d failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    await asyncio.sleep(1)
            finally:
                if temp_fd is not None:
                    try:
                        os.close(temp_fd)
                    except OSError:
                        pass
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass

        raise last_error  # type: ignore[misc]

    @staticmethod
    def _error(msg: str) -> dict:
        return {"success": False, "filepath": None, "title": None, "error": msg}


def _normalize_share_url(url: str) -> str:
    """Keep share URLs as-is; short-link resolution enforces HTTPS."""
    return url.strip()


def _positive_int(value) -> int:
    """Return a positive integer value, or zero for missing/invalid data."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _candidate_resolution(candidate: Mapping) -> tuple[int, int]:
    """Return (width, height), using the gear name when dimensions are absent."""
    width = _positive_int(candidate.get("width") or candidate.get("video_width"))
    height = _positive_int(candidate.get("height") or candidate.get("video_height"))
    if height:
        return width, height

    gear_name = str(candidate.get("gear_name") or "")
    match = re.search(r"(?<!\d)(\d{3,5})(?:p)?(?:_|$)", gear_name, re.IGNORECASE)
    return width, _positive_int(match.group(1)) if match else 0


def _candidate_quality_rank(candidate: Mapping) -> int:
    """Rank explicit Douyin gear families for equal-bitrate tie-breaking."""
    gear_name = str(candidate.get("gear_name") or "").strip().lower()
    if gear_name.startswith("high_"):
        return 4
    if gear_name.startswith("normal_"):
        return 3
    if gear_name.startswith("low_"):
        return 2
    if gear_name.startswith("lower_"):
        return 1
    return 0


def _candidate_codec_rank(candidate: Mapping) -> int:
    """Prefer HEVC only as a final tie-break at equal quality and bitrate."""
    codec = str(candidate.get("codec_type") or "").lower()
    if candidate.get("is_h265") in (1, True) or codec in {"hevc", "h265"}:
        return 1
    return 0


def _first_play_url(candidate: Mapping) -> str | None:
    play_addr = candidate.get("play_addr")
    if not isinstance(play_addr, Mapping):
        return None
    urls = play_addr.get("url_list")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, (list, tuple)):
        return None
    return next((url.strip() for url in urls if isinstance(url, str) and url.strip()), None)


def _select_best_video_url(video_data, fallback_urls) -> str | None:
    """Select the highest-quality playable stream from F2's raw candidates.

    Bitrate is the primary quality signal.  Gear family and dimensions make
    equal-bitrate choices deterministic, while the original index is retained
    as the final stable tie-break.  The convenience property remains a safe
    fallback for older or unexpected F2 response shapes.
    """
    candidates: list[tuple[tuple[int, int, int, int, int, int], str, Mapping]] = []
    try:
        raw = video_data._to_raw()
    except (AttributeError, TypeError, ValueError):
        raw = None

    aweme_detail = raw.get("aweme_detail") if isinstance(raw, Mapping) else None
    video = aweme_detail.get("video") if isinstance(aweme_detail, Mapping) else None
    raw_candidates = video.get("bit_rate") if isinstance(video, Mapping) else None
    if isinstance(raw_candidates, Mapping):
        raw_candidates = [raw_candidates]
    if isinstance(raw_candidates, (list, tuple)):
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, Mapping):
                continue
            url = _first_play_url(candidate)
            bit_rate = _positive_int(candidate.get("bit_rate"))
            if not url or not bit_rate:
                continue
            width, height = _candidate_resolution(candidate)
            sort_key = (
                bit_rate,
                _candidate_quality_rank(candidate),
                height,
                width,
                _candidate_codec_rank(candidate),
                -index,
            )
            candidates.append((sort_key, url, candidate))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        sort_key, selected_url, selected = candidates[0]
        width, height = _candidate_resolution(selected)
        logger.info(
            "Douyin stream candidates=%d selected gear=%s bitrate=%d resolution=%sx%s codec=%s",
            len(candidates),
            selected.get("gear_name") or "unknown",
            sort_key[0],
            width or "?",
            height or "?",
            "hevc" if _candidate_codec_rank(selected) else "h264/unknown",
        )
        return selected_url

    if isinstance(fallback_urls, str):
        fallback_urls = [fallback_urls]
    if isinstance(fallback_urls, (list, tuple)):
        fallback = next(
            (url.strip() for url in fallback_urls if isinstance(url, str) and url.strip()),
            None,
        )
        if fallback:
            logger.debug("Using F2 convenience video URL fallback")
            return fallback
    return None


def _download_timestamp() -> str:
    """Return a stable timestamp prefix for newly downloaded files."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _has_downloaded_content(filepath: Path) -> bool:
    """Return whether filepath is a usable, non-empty regular file."""
    try:
        return filepath.is_file() and filepath.stat().st_size > 0
    except OSError:
        return False


def _find_existing_slideshow_file(directory: Path, suffix: str) -> Path | None:
    """Reuse a completed slideshow item when a retry has a new timestamp."""
    suffix = suffix.lower()
    try:
        candidates = directory.iterdir()
    except OSError:
        return None
    for candidate in candidates:
        if candidate.name.lower().endswith(suffix) and _has_downloaded_content(candidate):
            return candidate
    return None


async def _resolve_short_link(path: str, headers: dict) -> str:
    """Resolve a Douyin short link over verified HTTPS only.

    The redirect is deliberately not followed automatically: only a known
    Douyin host and the exact video/note path shape are accepted and cached.
    A private CA may be supplied explicitly with ``DOUYIN_SHORT_LINK_CA_BUNDLE``.
    """
    if not DOUYIN_SHORT_PATH_RE.fullmatch(path):
        return ""

    short_url = f"https://v.douyin.com/{path}/"
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                headers=headers,
                verify=_short_link_verify(),
                trust_env=False,
            ) as client:
                response = await client.get(short_url)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if _validated_aweme_id(location):
                    logger.debug(
                        "Short link resolved via HTTPS on attempt %d: %s",
                        attempt,
                        location[:120],
                    )
                    return location
            logger.debug(
                "Short link returned no acceptable redirect on attempt %d: HTTP %s",
                attempt,
                response.status_code,
            )
        except httpx.TimeoutException:
            logger.debug("Short link HTTPS timed out on attempt %d", attempt)
        except httpx.HTTPError as exc:
            logger.debug("Short link HTTPS failed on attempt %d: %s", attempt, exc)
        except Exception:
            logger.debug("Short link HTTPS failed on attempt %d", attempt, exc_info=True)
        await asyncio.sleep(1)
    return ""


def _validated_aweme_id(location: str) -> str:
    """Return an aweme ID only for a verified HTTPS Douyin redirect target."""
    try:
        parsed = urlparse(location)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DOUYIN_REDIRECT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.netloc.endswith(":")
        or parsed.fragment
    ):
        return ""
    match = DOUYIN_AWEME_PATH_RE.fullmatch(parsed.path)
    return match.group(1) if match else ""


def _is_douyin_short_host_url(url: str) -> bool:
    """Identify v.douyin.com URLs so HTTP or malformed short links never fall back."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname == "v.douyin.com"
    except ValueError:
        return False


async def _resolve_aweme_id(url: str) -> str:
    """Resolve Douyin URLs without following short links onto flaky HTTPS hosts."""
    candidate = url.strip()
    direct_match = DOUYIN_AWEME_ID_RE.search(candidate)
    if direct_match:
        return direct_match.group(1)

    short_match = DOUYIN_SHORT_RE.fullmatch(candidate)
    if short_match:
        cache_key = _short_link_cache_key(short_match.group(1))
        cached_aweme_id = _read_cached_aweme_id(cache_key)
        if cached_aweme_id:
            logger.debug("Resolved short link from cache: %s -> %s", cache_key, cached_aweme_id)
            return cached_aweme_id

        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"}
        location = await _resolve_short_link(short_match.group(1), headers)
        aweme_id = _validated_aweme_id(location)
        if aweme_id:
            logger.debug("Resolved short link from Location header: %s", location)
            _write_cached_aweme_id(cache_key, aweme_id)
            return aweme_id
        raise APITimeoutError("Douyin short-link redirect timed out")

    if _is_douyin_short_host_url(candidate):
        raise APITimeoutError("Douyin short links require HTTPS and a valid short path")

    return await AwemeIdFetcher.get_aweme_id(candidate)


def _short_link_cache_key(path: str) -> str:
    return f"https://v.douyin.com/{path.strip('/')}/"


def _load_short_link_cache() -> dict:
    try:
        if not SHORT_LINK_CACHE_PATH.exists():
            return {}
        raw = SHORT_LINK_CACHE_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read short-link cache %s: %s", SHORT_LINK_CACHE_PATH, exc)
        return {}


def _read_cached_aweme_id(cache_key: str) -> str:
    item = _load_short_link_cache().get(cache_key, {})
    if not isinstance(item, dict):
        return ""
    if item.get("schema") != SHORT_LINK_CACHE_SCHEMA:
        return ""
    aweme_id = item.get("aweme_id", "")
    return aweme_id if isinstance(aweme_id, str) and aweme_id.isdigit() else ""


def _write_cached_aweme_id(cache_key: str, aweme_id: str) -> None:
    try:
        cache = _load_short_link_cache()
        cache[cache_key] = {
            "schema": SHORT_LINK_CACHE_SCHEMA,
            "aweme_id": aweme_id,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        SHORT_LINK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = SHORT_LINK_CACHE_PATH.with_suffix(SHORT_LINK_CACHE_PATH.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(SHORT_LINK_CACHE_PATH)
        logger.debug("Cached short link: %s -> %s", cache_key, aweme_id)
    except OSError as exc:
        logger.warning("Failed to write short-link cache %s: %s", SHORT_LINK_CACHE_PATH, exc)


async def _process_downloaded_media(
    filepath: Path, lock_root: Path | None = None
) -> None:
    """Run CPU/subprocess media work without blocking the downloader loop."""
    try:
        result = await asyncio.to_thread(
            process_media, filepath, lock_root=lock_root
        )
        log_process_result(result, logger)
    except Exception as exc:
        # Post-processing must never turn a completed download into a failure.
        logger.warning("Auto-crop failed for %s: %s", filepath.name, exc)


def _sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in file names."""
    unsafe = r'<>:"/\|?*'
    for ch in unsafe:
        name = name.replace(ch, "_")
    return name.strip()
