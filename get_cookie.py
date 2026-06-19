"""Get Douyin cookie from Firefox and write to .env.

Usage:
    uv run python get_cookie.py              # open Firefox for login
    uv run python get_cookie.py --no-open    # try existing cookies only (headless)
"""

import argparse
import os
import sys
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
ENV_PATH = PROJECT_DIR / ".env"


def main():
    parser = argparse.ArgumentParser(description="获取抖音 cookie 并写入 .env")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="不打开浏览器，直接尝试从已登录的 Firefox 提取 cookie",
    )
    args = parser.parse_args()

    if not args.no_open:
        print("正在打开 Firefox 访问抖音登录页...")
        webbrowser.open("https://www.douyin.com/")
        print()
        print("请在 Firefox 中登录抖音（扫码或账号密码），登录成功后回到这里按回车...")
        input()

    print("正在从 Firefox 提取 cookie...")
    cookie = _extract_from_firefox()

    if not cookie:
        print("✗ 未能从 Firefox 提取到抖音 cookie。")
        print()
        print("可能的原因：")
        print("  1. Firefox 未安装")
        print("  2. 未在 Firefox 中登录过 douyin.com")
        print("  3. Firefox 配置文件路径不标准")
        print()
        print("替代方案：")
        print("  1. 在 Firefox 中打开 https://www.douyin.com 并登录")
        print("  2. 按 F12 → 控制台 → 输入 document.cookie → 复制输出")
        print("  3. 手动编辑 .env 文件，粘贴到 DOUYIN_COOKIE= 后面")
        sys.exit(1)

    _write_env(ENV_PATH, "DOUYIN_COOKIE", cookie)
    print(f"✓ Cookie 已写入 .env ({len(cookie)} 字符)")
    print()
    print("有效期通常 24-48 小时。过期后再次运行此脚本即可。")


def _extract_from_firefox() -> str | None:
    """Extract douyin.com cookies from Firefox profile."""
    try:
        import browser_cookie3
    except ImportError:
        return None

    try:
        cj = browser_cookie3.firefox()
    except Exception:
        return None

    cookies = []
    for c in cj:
        if c.domain in {".douyin.com", "douyin.com", "www.douyin.com"}:
            cookies.append(f"{c.name}={c.value}")

    return "; ".join(cookies) if cookies else None


def _write_env(env_path: Path, key: str, value: str) -> None:
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
