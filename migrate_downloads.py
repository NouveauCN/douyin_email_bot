"""One-shot migration: flatten slideshow directories into downloads/slides/.

Phase 1: Move *_slides/ dirs from author folders into slides/ (if not already there).
Phase 2: Flatten slides/*_slides/ — extract files, rename with prefix, remove subdirs.

Usage:
    uv run python migrate_downloads.py              # execute migration
    uv run python migrate_downloads.py --dry-run    # preview without moving
    uv run python migrate_downloads.py -d ./my_downloads  # custom download dir
"""

import argparse
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def load_download_path(config_path: Path) -> str:
    """Read download_path from config.yaml, falling back to ./downloads."""
    if config_path.exists() and yaml is not None:
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            return cfg.get("douyin", {}).get("download_path", "./downloads")
        except Exception:
            pass
    return "./downloads"


def migrate(download_dir: Path, dry_run: bool = False) -> int:
    """Run both migration phases. Returns total count of moved items."""
    download_dir = download_dir.resolve()
    slides_dir = download_dir / "slides"

    if not download_dir.exists():
        print(f"[ERROR] Download dir not found: {download_dir}")
        return 0

    count = 0

    # ── Phase 1: Move *_slides/ from author dirs → slides/ ─────────
    dir_moves: list[tuple[Path, Path]] = []
    for entry in sorted(download_dir.iterdir()):
        if not entry.is_dir() or entry.name == "slides":
            continue
        for sub in sorted(entry.iterdir()):
            if sub.is_dir() and sub.name.endswith("_slides"):
                dst = slides_dir / sub.name
                dir_moves.append((sub, dst))

    if dir_moves:
        print(f"Phase 1: {len(dir_moves)} slideshow director{'y' if len(dir_moves) == 1 else 'ies'} to move:\n")
        for src, dst in dir_moves:
            print(f"  {src}\n    -> {dst}")
        print()

    if not dry_run:
        slides_dir.mkdir(parents=True, exist_ok=True)
        for src, dst in dir_moves:
            if dst.exists():
                print(f"[SKIP] Target exists: {dst}")
                continue
            try:
                shutil.move(str(src), str(dst))
                print(f"[OK] {src.name} -> slides/")
                count += 1
            except OSError as e:
                print(f"[ERROR] {src}: {e}")

        # Clean empty author dirs after phase 1
        for entry in sorted(download_dir.iterdir()):
            if not entry.is_dir() or entry.name == "slides":
                continue
            try:
                if not list(entry.iterdir()):
                    entry.rmdir()
                    print(f"[CLEAN] Removed empty dir: {entry.name}/")
            except OSError:
                pass

    # ── Phase 2: Flatten slides/*_slides/ → slides/*.ext ───────────
    file_moves: list[tuple[Path, Path]] = []  # (src, dst)
    dirs_to_remove: set[Path] = set()

    if slides_dir.exists():
        for entry in sorted(slides_dir.iterdir()):
            if not entry.is_dir() or not entry.name.endswith("_slides"):
                continue

            # Extract prefix: "20260622_7654183945810898297_slides" → "20260622_7654183945810898297"
            prefix = entry.name.removesuffix("_slides")

            for sub in sorted(entry.iterdir()):
                if sub.is_file():
                    new_name = f"{prefix}_{sub.name}"
                    dst = slides_dir / new_name
                    file_moves.append((sub, dst))

            dirs_to_remove.add(entry)

    if file_moves:
        print(f"Phase 2: {len(file_moves)} file{'s' if len(file_moves) != 1 else ''} to flatten from {len(dirs_to_remove)} subdirector{'ies' if len(dirs_to_remove) != 1 else 'y'}:\n")
        for src, dst in file_moves:
            print(f"  {src.name} -> {dst.name}")
        print()

    if dry_run:
        total = count + len(file_moves)
        if count:
            print(f"[DRY RUN] Phase 1: would move {count} director{'ies' if count != 1 else 'y'}.")
        if file_moves:
            print(f"[DRY RUN] Phase 2: would flatten {len(file_moves)} file{'s' if len(file_moves) != 1 else ''}.")
        print(f"[DRY RUN] Total actions: {total}")
        return total

    for src, dst in file_moves:
        if dst.exists():
            print(f"[SKIP] Target exists: {dst.name}")
            continue
        try:
            shutil.move(str(src), str(dst))
            print(f"[OK] {src.name} -> {dst.name}")
            count += 1
        except OSError as e:
            print(f"[ERROR] {src}: {e}")

    # Clean empty *_slides/ dirs after phase 2
    for d in sorted(dirs_to_remove, reverse=True):
        try:
            remaining = list(d.iterdir())
            if not remaining:
                d.rmdir()
                print(f"[CLEAN] Removed empty dir: slides/{d.name}/")
        except OSError:
            pass

    print(f"\nDone. {count} action{'s' if count != 1 else ''} completed.")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flatten slideshow directories into downloads/slides/")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without executing")
    parser.add_argument(
        "-d", "--download-dir", type=str, default=None,
        help="Download directory (default: from config.yaml or ./downloads)")
    args = parser.parse_args()

    project_dir = Path(__file__).parent
    if args.download_dir:
        download_dir = Path(args.download_dir)
    else:
        config_path = project_dir / "config.yaml"
        download_path = load_download_path(config_path)
        download_dir = project_dir / download_path

    count = migrate(download_dir, dry_run=args.dry_run)
    if count == 0:
        sys.exit(0)


if __name__ == "__main__":
    main()
