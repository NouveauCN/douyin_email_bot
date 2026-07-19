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


def main() -> int:
    parser = argparse.ArgumentParser(description="安全检测并裁掉媒体外缘的连续同色行列")
    parser.add_argument("target", type=Path, help="单个媒体文件或下载目录")
    parser.add_argument("--apply", action="store_true", help="实际修改；默认只预览")
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

    total = changed = reviews = failures = 0
    for path in _media_paths(args.target):
        total += 1
        try:
            result = process_media(
                path,
                dry_run=not args.apply,
                force_review=args.force_review,
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
