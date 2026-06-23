"""Play all downloaded videos in random order — one run, full playlist.

Each run generates a fresh random shuffle.  Videos play sequentially;
a background preloader warms the OS disk cache for the next 2–3 videos
so playback starts instantly.  Only .mp4 files are played (images skipped).

Usage:
    uv run python play.py                  # Play all videos in random order
    uv run python play.py --dry-run        # Show what would play (no player)
    uv run python play.py --player mpv     # Use a specific player
    uv run python play.py --preload N      # Preload N upcoming videos (default 3)
    uv run python play.py --resolution WxH # Lock window size (default 1280x720)
"""

import argparse
import collections
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

PROJECT_DIR = Path(__file__).parent
DEFAULT_DOWNLOAD_DIR = PROJECT_DIR / "downloads"

# Image extensions that exist alongside .mp4 in slideshow dirs — skipped
_IMAGE_EXTS = frozenset({".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp"})


# ── Video discovery ─────────────────────────────────────────────────

def find_videos(download_dir: Path) -> list[Path]:
    """Recursively collect all .mp4 files, skipping images.

    Slideshow directories contain both static images (.webp/.jpg) and
    animated clips (.mp4).  We only collect the .mp4 files.
    """
    if not download_dir.exists():
        return []

    videos: list[Path] = []
    for p in sorted(download_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".mp4":
            videos.append(p)
    return videos


# ── Preloader ───────────────────────────────────────────────────────

class VideoPreloader:
    """Background thread that pre-reads upcoming video files.

    Reading files into the OS page cache BEFORE the player opens them
    eliminates cold-cache stutter, especially on HDDs or network drives.
    """

    def __init__(self, count: int = 3):
        self._count = count
        self._queue: collections.deque[Path] = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────

    def set_upcoming(self, paths: list[Path]) -> None:
        """Replace the preload queue with new paths."""
        with self._lock:
            self._queue.clear()
            for p in paths[: self._count]:
                self._queue.append(p)

    def start(self) -> None:
        """Start the background preload thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and wait for it."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    # ── Thread body ───────────────────────────────────────────────

    def _run(self) -> None:
        chunk = 1_048_576  # 1 MiB
        while not self._stop.is_set():
            with self._lock:
                try:
                    path = self._queue.popleft()
                except IndexError:
                    path = None
            if path is None:
                time.sleep(0.15)
                continue
            if not path.exists():
                continue
            try:
                with open(path, "rb") as fh:
                    while fh.read(chunk):
                        if self._stop.is_set():
                            break
            except Exception:
                pass  # preload is best-effort


# ── Player detection ────────────────────────────────────────────────

# Known player install paths on Windows: name → [candidate_rel_paths].
# Order matters: first-found wins, and the list is checked before assoc/ftype.
_WIN_PLAYER_PATHS: dict[str, list[str]] = {
    "mpv": ["mpv/mpv.exe"],
    "vlc": ["VideoLAN/VLC/vlc.exe"],
    "mpc-hc64": ["MPC-HC/mpc-hc64.exe"],
    "mpc-hc": ["MPC-HC/mpc-hc.exe"],
    "potplayer": ["DAUM/PotPlayer/PotPlayerMini64.exe", "DAUM/PotPlayer/PotPlayerMini.exe"],
    "wmplayer": ["Windows Media Player/wmplayer.exe"],
}

# Players that support .m3u playlist files → one process, native transitions.
_PLAYLIST_PLAYERS = frozenset({"mpv", "vlc"})

# Sequential-only players that need per-file flags to force a new window
# (otherwise single-instance mode makes subprocess return immediately).
_SEQUENTIAL_BLOCKING_ARGS: dict[str, list[str]] = {
    "mpc-hc64": ["/new"],
    "mpc-hc":  ["/new"],
}

_DEFAULT_RESOLUTION = "1280x720"


def _window_size_args(player_name: str, resolution: str) -> list[str]:
    """Per-player CLI flags for fullscreen + video scaling.

    Fullscreen gives every video the same canvas; VLC/mpv's default
    autoscale stretches low-res content to fill as much of the screen
    as possible while preserving aspect ratio.
    """
    if player_name in ("mpv", "vlc"):
        return ["--fullscreen"]
    return []


def _detect_player(preferred: str | None = None) -> tuple[str | None, str]:
    """Return (player_exe | None, mode).

    mode values:
      "playlist"   — supports .m3u (mpv, vlc) → one process, all videos
      "explicit"   — known player .exe → sequential one-by-one
      "system"     — OS default handler (fallback)
    """
    if preferred:
        found = _which(preferred)
        if found:
            return found, "playlist" if preferred in _PLAYLIST_PLAYERS else "explicit"
        print(f"{Fore.YELLOW}未找到播放器 '{preferred}'，回退到自动检测")

    # 1. Scan known player install paths on Windows
    if sys.platform == "win32":
        for name, rel_paths in _WIN_PLAYER_PATHS.items():
            exe = _find_player_exe(name, rel_paths)
            if exe:
                return exe, "playlist" if name in _PLAYLIST_PLAYERS else "explicit"

        # 2. Resolve .mp4 handler via assoc/ftype (may find VLC etc.)
        handler = _find_mp4_handler()
        if handler:
            name = Path(handler).stem.lower()
            return handler, "playlist" if name in _PLAYLIST_PLAYERS else "explicit"

    # 3. Check PATH for mpv (macOS/Linux)
    mpv = _which("mpv")
    if mpv:
        return mpv, "playlist"

    # 4. OS default
    return None, "system"


def _which(name: str) -> str | None:
    """Check if a command exists on PATH.  Returns path or None."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["where", name], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                line = result.stdout.strip().splitlines()[0].strip()
                return line
        except Exception:
            pass
        return None
    try:
        result = subprocess.run(
            ["which", name], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _find_player_exe(name: str, rel_paths: list[str]) -> str | None:
    """Search for a player .exe in known install directories."""
    exe = _which(name)
    if exe:
        return exe
    bases = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.expandvars(r"%LOCALAPPDATA%"),
        os.path.expandvars(r"%APPDATA%"),
    ]
    for base in bases:
        for rel in rel_paths:
            candidate = Path(base) / rel
            if candidate.is_file():
                return str(candidate)
    return None


def _find_mp4_handler() -> str | None:
    """Resolve the registered .mp4 handler via assoc → ftype → .exe path."""
    try:
        assoc = subprocess.run(
            ["cmd", "/c", "assoc", ".mp4"],
            capture_output=True, text=True, timeout=5,
        )
        if assoc.returncode != 0:
            return None
        parts = assoc.stdout.strip().split("=", 1)
        if len(parts) < 2:
            return None
        progid = parts[1].strip()
        if not progid:
            return None

        ftype = subprocess.run(
            ["cmd", "/c", "ftype", progid],
            capture_output=True, text=True, timeout=5,
        )
        if ftype.returncode != 0:
            return None
        fparts = ftype.stdout.strip().split("=", 1)
        if len(fparts) < 2:
            return None
        cmdline = fparts[1].strip()

        if cmdline.startswith('"'):
            exe = cmdline.split('"')[1]
        else:
            exe = cmdline.split()[0]

        if Path(exe).is_file():
            return exe
    except Exception:
        pass
    return None


# ── Playback orchestrator ───────────────────────────────────────────

def play_all(
    videos: list[Path],
    player_cmd: str | None = None,
    preload_count: int = 3,
    dry_run: bool = False,
    resolution: str = _DEFAULT_RESOLUTION,
) -> None:
    """Play all videos in order.

    Args:
        videos: Ordered list of .mp4 files to play.
        player_cmd: Override player command.  None = auto-detect.
        preload_count: Number of upcoming videos to preload into OS cache
                       (sequential mode only; playlist mode handles its own buffer).
        dry_run: Print queue and exit without playing.
        resolution: Fixed window size as WxH (e.g. "1280x720"). Prevents
                    the player window from resizing across videos.
    """
    total = len(videos)
    if total == 0:
        print(f"{Fore.YELLOW}没有找到可播放的视频 (.mp4)")
        return

    player_exe, mode = _detect_player(player_cmd)

    if dry_run:
        _print_dry_run(videos, player_exe or _mode_label(mode), mode, preload_count, resolution)
        return

    # ── Playlist mode (mpv / VLC) — one process, all videos ─────
    if mode == "playlist":
        _play_with_playlist(videos, player_exe, resolution)
        return

    # ── Sequential mode — one-by-one with preloader ─────────────
    _play_sequential(videos, player_exe, preload_count, resolution)


# ── Playlist mode (mpv / VLC) ──────────────────────────────────────

def _play_with_playlist(
    videos: list[Path],
    player_exe: str | None,
    resolution: str = _DEFAULT_RESOLUTION,
) -> None:
    """Write a temp .m3u file and launch the player with it.

    Both mpv and VLC support .m3u playlists.  The player handles
    transitions and pre-buffering natively — no preloader needed.
    """
    import tempfile

    player_exe = player_exe or "mpv"
    player_name = Path(player_exe).stem.lower()
    win_args = _window_size_args(player_name, resolution)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".m3u", encoding="utf-8", delete=False,
    ) as f:
        f.write("#EXTM3U\n")
        for v in videos:
            f.write(str(v) + "\n")
        playlist_path = f.name

    total = len(videos)
    print(f"{Fore.CYAN}{Style.BRIGHT}▶ {player_name} 播放列表模式 — {total} 个视频")
    if win_args:
        print(f"  全屏播放")
    for i, v in enumerate(videos):
        try:
            rel = v.relative_to(PROJECT_DIR)
        except ValueError:
            rel = v
        print(f"  {i + 1:3d}. {rel.name}")
    print(f"  种子: {_seed_repr()}")
    print(f"  {Fore.YELLOW}Ctrl+C 或关闭播放器窗口退出")
    print()

    # mpv: --playlist= flag; VLC: opens .m3u directly, --play-and-exit
    if player_name == "mpv":
        cmd = [player_exe] + win_args + [
            f"--playlist={playlist_path}",
            "--keep-open=no",
            "--image-display-duration=0",
        ]
    else:
        cmd = [player_exe] + win_args + ["--play-and-exit", playlist_path]

    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}播放中断")
    finally:
        try:
            os.unlink(playlist_path)
        except OSError:
            pass

    print(f"\n{Fore.GREEN}{Style.BRIGHT}━━━ 播放完毕 ━━━")
    print(f"  总计: {total} 个视频")


# ── Sequential mode (one-by-one for non-playlist players) ──────────

def _play_sequential(
    videos: list[Path],
    player_exe: str | None,
    preload_count: int,
    resolution: str = _DEFAULT_RESOLUTION,
) -> None:
    """Play videos one at a time with background preloading."""
    total = len(videos)
    preloader = VideoPreloader(preload_count)

    print(f"{Fore.CYAN}{Style.BRIGHT}随机播放 {total} 个视频")
    print(f"  播放器: {player_exe or '系统默认'}")
    print(f"  预加载: {preload_count} 个")
    if player_exe:
        player_name = Path(player_exe).stem.lower()
        if player_name in ("mpv", "vlc"):
            print(f"  全屏播放")
    print(f"  Ctrl+C 跳过当前  |  Ctrl+C 两次退出")
    print()

    skipped: list[Path] = []
    consecutive_interrupts = 0

    for i, video in enumerate(videos):
        # ── Preload upcoming ────────────────────────────────────
        upcoming = videos[i + 1 : i + 1 + preload_count]
        preloader.set_upcoming(upcoming)
        preloader.start()

        # ── Display ─────────────────────────────────────────────
        try:
            rel = video.relative_to(PROJECT_DIR)
        except ValueError:
            rel = video

        file_size_mb = video.stat().st_size / 1_000_000 if video.exists() else 0

        print(
            f"{Fore.CYAN}{Style.BRIGHT}▶ [{i + 1}/{total}]"
            f"{Style.RESET_ALL} {rel.name}"
        )
        print(f"     {Fore.WHITE}{rel.parent}")
        print(f"     {Fore.WHITE}{file_size_mb:.1f} MB")
        if upcoming:
            labels = [f"  → {u.name}" for u in upcoming[:3]]
            print(f"     {Fore.YELLOW}预加载:{Style.DIM}" + "\n".join(labels))
        print()

        # ── Play ────────────────────────────────────────────────
        consecutive_interrupts = 0
        try:
            ok = _play_one(video, player_exe, resolution)
        except KeyboardInterrupt:
            consecutive_interrupts += 1
            if consecutive_interrupts >= 2:
                print(f"\n{Fore.YELLOW}再次 Ctrl+C，退出播放")
                break
            print(f"\n{Fore.YELLOW}⏭ 跳过 (Ctrl+C 再按一次退出)")
            skipped.append(video)
            preloader.stop()
            continue

        preloader.stop()

        if not ok:
            print(f"{Fore.RED}  播放失败，继续下一个...")
            skipped.append(video)

        print()

    # ── Summary ────────────────────────────────────────────────────
    played = total - len(skipped)
    print(f"{Fore.GREEN}{Style.BRIGHT}━━━ 播放完毕 ━━━")
    print(f"  已播放: {played}/{total}")
    if skipped:
        print(f"  已跳过: {len(skipped)}")
        for v in skipped:
            print(f"    - {v.name}")
    print(f"  种子:   {_seed_repr()}")


# ── Single-video playback ──────────────────────────────────────────

def _play_one(
    filepath: Path,
    player_cmd: str | None,
    resolution: str = _DEFAULT_RESOLUTION,
) -> bool:
    """Launch a single video and block until the player exits.

    Returns True on success, False on failure.
    """
    path_str = str(filepath)

    if player_cmd:
        player_name = Path(player_cmd).stem.lower()
        extra_args = _SEQUENTIAL_BLOCKING_ARGS.get(player_name, [])
        win_args = _window_size_args(player_name, resolution)
        cmd = [player_cmd] + extra_args + win_args + [path_str]
    elif sys.platform == "win32":
        cmd = ["cmd", "/c", "start", "", "/wait", path_str]
    elif sys.platform == "darwin":
        cmd = ["open", "-W", path_str]
    else:
        cmd = ["xdg-open", path_str]

    try:
        subprocess.run(cmd, check=False)
        return True
    except FileNotFoundError:
        print(f"{Fore.RED}播放器未找到: {cmd[0]}")
        print("请使用 --player 指定播放器，如: --player mpv")
        return False
    except Exception as exc:
        print(f"{Fore.RED}播放异常: {exc}")
        return False


# ── Output helpers ─────────────────────────────────────────────────

def _mode_label(mode: str) -> str:
    return {
        "playlist": "播放列表模式",
        "explicit": "逐一播放",
        "system": "系统默认",
    }.get(mode, mode)


def _print_dry_run(
    videos: list[Path],
    player_label: str,
    mode: str,
    preload_count: int,
    resolution: str = "",
) -> None:
    """Print the shuffled queue without playing."""
    print(f"{Fore.CYAN}{Style.BRIGHT}试运行 — {len(videos)} 个视频")
    print(f"  播放器: {player_label}  |  模式: {mode}  |  预加载: {preload_count}")
    if mode == "playlist":
        print(f"  全屏播放")
    print(f"  种子:   {_seed_repr()}")
    print()

    total_size = 0
    for i, v in enumerate(videos):
        try:
            rel = v.relative_to(PROJECT_DIR)
        except ValueError:
            rel = v
        sz = v.stat().st_size if v.exists() else 0
        total_size += sz
        print(f"  {i + 1:3d}. {Fore.GREEN}{rel}{Style.RESET_ALL}  ({sz / 1_000_000:.1f} MB)")

    print()
    print(f"  总大小: {total_size / 1_000_000:.1f} MB")


# ── Seed ────────────────────────────────────────────────────────────

# Module-level seed generated once per process invocation.
# Uses os.urandom for cryptographic-quality randomness.
_SEED = int.from_bytes(os.urandom(8), "big")
_SEED_TIME = time.strftime("%Y-%m-%d %H:%M:%S")


def _seed_repr() -> str:
    return f"{_SEED:016x} (generated at {_SEED_TIME})"


def _get_seed() -> int:
    return _SEED


# ── CLI ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="随机播放全部已下载视频（每次运行新随机种子，预加载缓冲）",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="仅显示播放列表，不实际播放",
    )
    parser.add_argument(
        "--player", "-p",
        default=None,
        help="指定播放器（如 mpv、vlc），默认自动检测 mpv 或使用系统关联",
    )
    parser.add_argument(
        "--preload", "-P",
        type=int,
        default=3,
        metavar="N",
        help="预加载下 N 个视频到系统缓存（默认 3）",
    )
    parser.add_argument(
        "--download-dir", "-d",
        type=Path,
        default=None,
        help=f"下载目录（默认: {DEFAULT_DOWNLOAD_DIR}）",
    )
    parser.add_argument(
        "--ignore", "-I",
        type=Path,
        default=None,
        metavar="VIDEO_PATH",
        help="跳过指定视频（可多次使用）",
        action="append",
    )
    parser.add_argument(
        "--resolution", "-R",
        default=_DEFAULT_RESOLUTION,
        metavar="WxH",
        help=f"强制播放器窗口大小，避免随视频分辨率变化（默认: {_DEFAULT_RESOLUTION}）",
    )
    args = parser.parse_args()

    download_dir = args.download_dir or DEFAULT_DOWNLOAD_DIR

    # ── Discover videos ─────────────────────────────────────────────
    videos = find_videos(download_dir)

    # ── Apply --ignore filters ──────────────────────────────────────
    if args.ignore:
        ignore_set = {str(p.resolve()) for p in args.ignore}
        before = len(videos)
        videos = [v for v in videos if str(v) not in ignore_set]
        if len(videos) < before:
            print(f"{Fore.YELLOW}已跳过 {before - len(videos)} 个指定视频")

    if not videos:
        print(f"{Fore.YELLOW}下载目录中未找到 .mp4 文件: {download_dir}")
        print("提示：视频下载到 config.yaml 中 douyin.download_path 指定的目录")
        return

    # ── Shuffle with fresh per-run seed ─────────────────────────────
    rng = random.Random(_SEED)
    rng.shuffle(videos)

    # ── Play ────────────────────────────────────────────────────────
    play_all(
        videos=videos,
        player_cmd=args.player,
        preload_count=max(1, args.preload),
        dry_run=args.dry_run,
        resolution=args.resolution,
    )


if __name__ == "__main__":
    main()
