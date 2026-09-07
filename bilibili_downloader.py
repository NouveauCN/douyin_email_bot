"""Bilibili downloader wrapper using yutto's CLI."""

import logging
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from colorama import Fore, Style
import httpx

from media_file_lock import MediaFileLockBusy, media_file_lock
from media_processor import log_process_result, process_media

logger = logging.getLogger("BilibiliDownloader")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TITLE_RE = re.compile(r"《([^》]+)》")
_MEDIA_EXTS = {".mp4", ".mkv", ".mov"}
_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_ID_RE = re.compile(r"(?:/video/(BV[0-9A-Za-z]+|av[0-9]+)|[?&](?:bvid|aid)=(BV[0-9A-Za-z]+|[0-9]+))")
_AUTHOR_MAX_LENGTH = 50
_B23_HOSTS = {"b23.tv", "www.b23.tv"}
_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com"}


class BilibiliDownloader:
    """Download Bilibili videos through yutto.

    yutto is intentionally invoked as a subprocess instead of imported as a
    library; its public contract is the CLI and this keeps our integration
    insulated from internal API churn.
    """

    def __init__(self, config):
        self.config = config

    def download(self, url: str) -> dict:
        """Download a single Bilibili URL.

        Returns a dict compatible with DouyinDownloader.download().
        """
        download_dir = Path(self.config.download_path)
        download_dir.mkdir(parents=True, exist_ok=True)
        shared_root = download_dir.parent
        transaction_lock = media_file_lock(
            download_dir / ".yutto-transaction",
            root=shared_root,
            timeout=max(5.0, float(self.config.timeout)),
        )
        try:
            transaction_lock.acquire()
        except MediaFileLockBusy:
            return self._error("B站下载等待媒体目录锁超时，请稍后重试")
        try:
            return self._download_locked(url, download_dir, shared_root)
        finally:
            transaction_lock.release()

    def _download_locked(self, url: str, download_dir: Path, shared_root: Path) -> dict:
        """Run yutto and publish results while browser media writes are paused."""
        started_at = time.time()
        command = self._build_command(url, download_dir)
        logger.info("Running yutto for Bilibili URL: %s", url)
        logger.debug("yutto command: %s", _redact_command(command))

        try:
            completed = subprocess.run(
                command,
                cwd=download_dir,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Bilibili download timed out after %ds: %s", self.config.timeout, url)
            return self._error("B站下载超时，请稍后重试或调大 bilibili.timeout")
        except OSError as exc:
            logger.warning("Failed to run yutto: %s", exc)
            return self._error(
                f"无法启动 yutto：{exc}。请在主项目环境外安装 yutto，"
                "或配置 bilibili.yutto_bin / BILIBILI_YUTTO_BIN"
            )

        output = _strip_ansi("\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ))

        if completed.returncode != 0:
            logger.warning("yutto failed with code %s: %s", completed.returncode, output[-2000:])
            return self._error(_summarize_yutto_error(output))

        covers = _move_cover_files(download_dir, started_at)
        files = _collect_downloaded_files(download_dir, started_at)
        video_id = _extract_video_id(url)
        author, _ = _fetch_bilibili_metadata(video_id)
        files = _rename_downloaded_files(
            files,
            download_dir,
            video_id or "unknown",
            author,
            _download_timestamp(started_at),
        )
        _process_downloaded_media([*files, *covers], shared_root)
        filepath = _format_file_result(files, download_dir)
        title = _extract_title(output) or "Bilibili Video"

        logger.info(
            f"{Fore.GREEN}{Style.BRIGHT}[DONE] B站下载完成: %s -> %s (%d file%s)",
            title,
            filepath or download_dir,
            len(files),
            "" if len(files) == 1 else "s",
        )
        if covers:
            logger.info("B站封面已保存到 slides: %s", ", ".join(str(path) for path in covers))

        return {
            "success": True,
            "filepath": filepath or str(download_dir),
            "files": [str(path) for path in files],
            "file_count": len(files),
            "covers": [str(path) for path in covers],
            "title": title,
            "error": None,
        }

    def _build_command(self, url: str, download_dir: Path) -> list[str]:
        yutto_bin = self.config.yutto_bin or "yutto"
        command = [
            yutto_bin,
            url,
            "--dir",
            str(download_dir),
            "--output-format",
            "mp4",
            "--no-progress",
            "--no-color",
            "--no-danmaku",
            "--no-subtitle",
            "--save-cover",
            "--download-vcodec-priority",
            "hevc,avc,av1",
        ]

        if self.config.auth:
            command.extend(["--auth", self.config.auth])
        elif self.config.auth_file and Path(self.config.auth_file).exists():
            command.extend(["--auth-file", self.config.auth_file])
        if self.config.video_quality:
            command.extend(["--video-quality", str(self.config.video_quality)])
        if self.config.batch:
            command.append("--batch")

        return command

    @staticmethod
    def _error(msg: str) -> dict:
        return {
            "success": False,
            "filepath": None,
            "files": [],
            "file_count": 0,
            "covers": [],
            "title": None,
            "error": msg,
        }


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _redact_command(command: list[str]) -> list[str]:
    redacted = command[:]
    for i, item in enumerate(redacted[:-1]):
        if item == "--auth":
            redacted[i + 1] = "<redacted>"
    return redacted


def _extract_title(output: str) -> str | None:
    match = _TITLE_RE.search(output)
    if match:
        return match.group(1).strip()
    return None


def _extract_video_id(url: str) -> str | None:
    """Extract the stable BV/av identifier without depending on yutto output."""
    value = _extract_video_id_from_url(url)
    if value:
        return value
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _B23_HOSTS:
        return None
    try:
        resolved = _resolve_b23_url(url)
    except Exception as exc:
        logger.info("Bilibili short-link resolution unavailable: %s", exc)
        return None
    return _extract_video_id_from_url(resolved) if resolved else None


def _extract_video_id_from_url(url: str) -> str | None:
    match = _VIDEO_ID_RE.search(url)
    if not match:
        return None
    value = next((group for group in match.groups() if group), "")
    if value.startswith("BV"):
        return value
    return f"av{value}" if value.isdigit() else value


def _resolve_b23_url(url: str) -> str | None:
    """Resolve a b23.tv link through a tiny, HTTPS-only redirect budget."""
    current = url
    for _ in range(3):
        parsed = urlparse(current)
        if parsed.scheme != "https" or parsed.hostname not in (_B23_HOSTS | _BILIBILI_HOSTS):
            return None
        if parsed.hostname in _BILIBILI_HOSTS:
            return current
        response = httpx.get(
            current,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=False,
            timeout=10,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return current
        location = response.headers.get("location")
        if not location:
            return None
        current = urljoin(current, location)
    return None


def _fetch_bilibili_metadata(video_id: str | None) -> tuple[str, None]:
    """Best-effort author lookup; metadata must not affect download success."""
    if not video_id:
        return "Bilibili", None
    params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    try:
        response = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return "Bilibili", None
        data = payload.get("data")
        if not isinstance(data, dict):
            return "Bilibili", None
        owner = data.get("owner")
        if not isinstance(owner, dict):
            return "Bilibili", None
        author = _sanitize_author(owner.get("name"))
        return author or "Bilibili", None
    except Exception as exc:
        logger.info("Bilibili metadata unavailable for %s: %s", video_id, exc)
        return "Bilibili", None


def _sanitize_author(name: object) -> str:
    """Match Douyin's filename sanitization and author length limit."""
    if not isinstance(name, str):
        return ""
    value = name
    unsafe = r'<>:"/\\|?*'
    for char in unsafe:
        value = value.replace(char, "_")
    return value.strip()[:_AUTHOR_MAX_LENGTH]


def _download_timestamp(timestamp: float | None = None) -> str:
    moment = datetime.now() if timestamp is None else datetime.fromtimestamp(timestamp)
    return moment.strftime("%Y%m%d_%H%M%S")


def _rename_downloaded_files(
    files: list[Path], download_dir: Path, video_id: str, author: str,
    timestamp: str,
) -> list[Path]:
    """Publish only this yutto invocation's videos under the Douyin layout."""
    if not files:
        return files
    author_dir = _sanitize_author(author) or "Bilibili"
    target_dir = download_dir / author_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    multiple = len(files) > 1
    renamed: list[Path] = []
    for index, source in enumerate(files, start=1):
        if not source.exists():
            # Keeps unit-test doubles and an already-published path harmless.
            renamed.append(source)
            continue
        part = f"_P{index:02d}" if multiple else ""
        target = _unique_path(target_dir / f"{timestamp}_{video_id}{part}.mp4")
        source.replace(target)
        renamed.append(target)
    return renamed


def _summarize_yutto_error(output: str) -> str:
    if "No module named yutto" in output:
        return "未安装 yutto CLI，请先运行 uv tool install yutto 或配置 BILIBILI_YUTTO_BIN"
    if "SESSDATA" in output or "登录" in output or "auth" in output.lower():
        return "B站下载失败，可能需要登录 Cookie；请配置 BILIBILI_AUTH"
    if "ffmpeg" in output.lower():
        return "B站下载失败：未找到或无法使用 ffmpeg"
    tail = "\n".join(line for line in output.splitlines() if line.strip())[-1000:]
    return tail or "B站下载失败，yutto 未返回详细错误"


def _collect_downloaded_files(download_dir: Path, started_at: float) -> list[Path]:
    files: list[Path] = []
    for path in download_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _MEDIA_EXTS:
            continue
        try:
            if path.stat().st_mtime >= started_at - 1:
                files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda p: p.stat().st_mtime)


def _move_cover_files(download_dir: Path, started_at: float) -> list[Path]:
    covers = _collect_files_by_ext(download_dir, started_at, _COVER_EXTS)
    if not covers:
        return []

    slides_dir = download_dir.parent / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    for cover in covers:
        target = _unique_path(slides_dir / f"bilibili_{cover.name}")
        try:
            # yutto has already exited; lock only the source-to-slides
            # publication, never the subprocess itself.
            with media_file_lock(cover, root=download_dir.parent, timeout=5):
                shutil.move(str(cover), str(target))
        except MediaFileLockBusy:
            logger.warning("Bilibili cover is busy, leaving it in place: %s", cover)
            continue
        except OSError as exc:
            logger.warning("Failed to move Bilibili cover %s to %s: %s", cover, target, exc)
            continue
        moved.append(target)
    return moved


def _collect_files_by_ext(download_dir: Path, started_at: float, exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for path in download_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        try:
            if path.stat().st_mtime >= started_at - 1:
                files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda p: p.stat().st_mtime)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _format_file_result(files: list[Path], download_dir: Path) -> str | None:
    if not files:
        return None
    if len(files) == 1:
        return str(files[0])

    parents = {path.parent for path in files}
    if len(parents) == 1:
        return str(next(iter(parents)))
    return f"{download_dir} ({len(files)} 个文件)"


def _process_downloaded_media(paths: list[Path], lock_root: Path | None = None) -> None:
    """Best-effort post-processing that cannot invalidate a yutto download."""
    for path in paths:
        try:
            result = process_media(path, lock_root=lock_root)
            log_process_result(result, logger)
        except Exception as exc:
            logger.warning("Auto-crop failed for %s: %s", path.name, exc)
