"""Dry-run or apply conservative border removal to existing downloaded media."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from media_processor import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    log_process_result,
    process_media,
)


def _media_paths(target: Path):
    if target.is_file():
        yield target
        return
    supported = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    for path in sorted(target.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in supported
            and not path.name.endswith("_original.bak")
        ):
            yield path


def _resolve_lock_root(
    target: Path,
    *,
    explicit_root: Path | None = None,
    configured_root: Path | None = None,
) -> Path:
    """Choose the same shared root used by the bot when the target is inside it."""
    target = target.expanduser().resolve()
    if explicit_root is not None:
        root = explicit_root.expanduser().resolve()
        target.relative_to(root)
        return root
    if configured_root is not None:
        root = configured_root.expanduser().resolve()
        try:
            target.relative_to(root)
        except ValueError:
            pass
        else:
            return root
    return target if target.is_dir() else target.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="安全检测并裁掉媒体外缘的连续同色行列")
    parser.add_argument("target", type=Path, help="单个媒体文件或下载目录")
    parser.add_argument("--apply", action="store_true", help="实际修改；默认只预览")
    parser.add_argument(
        "--lock-root",
        type=Path,
        help="共享下载根；默认优先使用 config.yaml 中的抖音下载目录",
    )
    parser.add_argument(
        "--force-review",
        action="store_true",
        help="处理需要人工确认的大面积裁剪候选；必须与 --apply 一起使用",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.target.exists():
        parser.error(f"路径不存在: {args.target}")
    if args.force_review and not args.apply:
        parser.error("--force-review 必须与 --apply 一起使用")

    configured_root = None
    if args.lock_root is None:
        try:
            from config_loader import load_config

            configured_root = Path(
                load_config(Path(__file__).parent / "config.yaml").douyin.download_path
            )
        except Exception as exc:
            logging.getLogger("MediaProcessor").warning(
                "无法读取配置下载根，将使用目标所在目录：%s", exc
            )
    try:
        lock_root = _resolve_lock_root(
            args.target,
            explicit_root=args.lock_root,
            configured_root=configured_root,
        )
    except ValueError:
        parser.error(f"目标不在锁根内: {args.lock_root}")

    total = changed = reviews = failures = 0
    for path in _media_paths(args.target):
        total += 1
        try:
            result = process_media(
                path,
                dry_run=not args.apply,
                force_review=args.force_review,
                lock_root=lock_root,
            )
            log_process_result(result)
            changed += int(result.changed)
            reviews += int(result.requires_review)
        except Exception as exc:
            failures += 1
            logging.getLogger("MediaProcessor").error("处理失败 %s: %s", path, exc)

    mode = "实际处理" if args.apply else "预览"
    print(
        f"{mode}完成：扫描 {total} 个文件，{changed} 个可裁剪，"
        f"{reviews} 个待人工确认，{failures} 个失败"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
