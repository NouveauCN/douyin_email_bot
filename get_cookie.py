"""Get Douyin cookie via Playwright Firefox and write to .env.

Supports both interactive login (visible browser) and headless
re-extraction from a persistent profile.

Usage:
    uv run python get_cookie.py              # interactive login (visible Firefox)
    uv run python get_cookie.py --headless   # headless re-extraction from profile
    uv run python get_cookie.py --no-validate  # skip cookie validation
    uv run python get_cookie.py --profile PATH  # custom profile directory
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

from cookie_extractor import (
    DEFAULT_PROFILE_DIR,
    extract_cookies,
    extract_with_playwright,
    validate_cookie,
)

PROJECT_DIR = Path(__file__).parent
ENV_PATH = PROJECT_DIR / ".env"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("get_cookie")


def interactive_login(profile_dir: Path, validate: bool = True) -> tuple[str | None, str]:
    """Launch visible Firefox, wait for user to log in, extract cookies.

    The profile persists, enabling future headless runs.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        return None, (
            "Playwright 未安装。请运行：\n"
            "  uv add playwright\n"
            "  playwright install firefox"
        )

    profile_dir.mkdir(parents=True, exist_ok=True)

    log.info("正在启动 Firefox (持久化配置: %s)...", profile_dir)
    log.info("提示：本窗口关闭或按 Enter 后，Firefox 会自动关闭。")

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 720},
            )

            page = browser.pages[0] if browser.pages else browser.new_page()

            try:
                page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as exc:
                log.warning("页面加载可能不完整: %s", exc)

            print()
            print(
                f"{Fore.CYAN}{Style.BRIGHT}"
                "Firefox 已打开抖音首页，请扫码登录。"
            )
            print("登录成功后，在网页上确认可以看到你的抖音主页。")
            print()
            input(f"{Fore.YELLOW}按 Enter 提取 cookie (Firefox 将自动关闭)...")

            cookies = browser.cookies()
            browser.close()

            douyin_cookies = _filter_douyin(cookies)
            if not douyin_cookies:
                return None, "未找到抖音 cookie，请确认已成功登录。"

            cookie_str = "; ".join(douyin_cookies)
            log.info("提取到 %d 个抖音 cookie", len(douyin_cookies))

            if validate:
                print(f"{Fore.CYAN}正在验证 cookie...")
                valid, reason = validate_cookie(cookie_str)
                if not valid:
                    return None, f"Cookie 无效：{reason}"
                print(f"{Fore.GREEN}验证通过: {reason}")

            return cookie_str, f"交互式登录成功（{len(cookie_str)} 字符）"

    except Exception as exc:
        return None, f"浏览器启动失败: {exc}"


def _filter_douyin(cookies: list) -> list[str]:
    """Filter and format douyin.com cookies from Playwright cookie dicts."""
    douyin_domains = frozenset({".douyin.com", "douyin.com", "www.douyin.com"})
    return [
        f"{c['name']}={c['value']}"
        for c in cookies
        if c.get("domain", "") in douyin_domains
    ]


def _write_env(env_path: Path, key: str, value: str) -> None:
    """Update or add a key=value line in a dotenv file."""
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


def main():
    parser = argparse.ArgumentParser(
        description="获取抖音 cookie 并写入 .env",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式：使用已登录的持久化配置静默提取 cookie",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="跳过 cookie 有效性验证",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help=f"Firefox 配置目录 (默认: {DEFAULT_PROFILE_DIR})",
    )
    args = parser.parse_args()

    profile_dir = args.profile or DEFAULT_PROFILE_DIR
    validate = not args.no_validate

    if args.headless:
        print(f"{Fore.CYAN}无头模式：从持久化配置中提取 cookie...")
        print(f"配置目录: {profile_dir}")
        cookie, msg = extract_cookies(
            profile_dir=profile_dir,
            headless=True,
            validate=validate,
        )
    else:
        print(f"{Fore.CYAN}交互模式：启动 Firefox 进行登录...")
        cookie, msg = interactive_login(profile_dir, validate=validate)

    if cookie:
        _write_env(ENV_PATH, "DOUYIN_COOKIE", cookie)
        print()
        print(f"{Fore.GREEN}{Style.BRIGHT}[DONE] Cookie 已写入 .env ({len(cookie)} 字符)")
        print(f"来源: {msg}")
        print()
        print("提示：Cookie 有效期通常 24-48 小时。")
        print("过期后运行 'uv run python get_cookie.py --headless' 尝试重新提取，")
        print("或运行 'uv run python get_cookie.py' 重新登录。")
    else:
        print()
        print(f"{Fore.RED}X 获取失败: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
