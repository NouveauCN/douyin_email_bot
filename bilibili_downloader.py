"""Bilibili downloader wrapper using yutto's CLI."""

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from colorama import Fore, Style

from media_processor import log_process_result, process_media

logger = logging.getLogger("BilibiliDownloader")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TITLE_RE = re.compile(r"《([^》]+)》")
_MEDIA_EXTS = {".mp4", ".mkv", ".mov"}
_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


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
        except ModuleNotFoundError:
            return self._error("未安装 yutto，请先运行 uv sync 或 pip install -r requirements.txt")
        except subprocess.TimeoutExpired:
            logger.warning("Bilibili download timed out after %ds: %s", self.config.timeout, url)
            return self._error("B站下载超时，请稍后重试或调大 bilibili.timeout")
        except OSError as exc:
            logger.warning("Failed to run yutto: %s", exc)
            return self._error(f"无法启动 yutto：{exc}")

        output = _strip_ansi("\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ))

        if completed.returncode != 0:
            logger.warning("yutto failed with code %s: %s", completed.returncode, output[-2000:])
            return self._error(_summarize_yutto_error(output))

        covers = _move_cover_files(download_dir, started_at)
        files = _collect_downloaded_files(download_dir, started_at)
        _process_downloaded_media([*files, *covers])
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
            shutil.move(str(cover), str(target))
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


def _process_downloaded_media(paths: list[Path]) -> None:
    """Best-effort post-processing that cannot invalidate a yutto download."""
    for path in paths:
        try:
            result = process_media(path)
            log_process_result(result, logger)
        except Exception as exc:
            logger.warning("Auto-crop failed for %s: %s", path.name, exc)
