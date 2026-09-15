"""Get Douyin cookie via Playwright Firefox and save it to runtime settings.

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
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

from cookie_extractor import (
    DEFAULT_PROFILE_DIR,
    collect_douyin_cookies,
    extract_cookies_with_user_agent,
    validate_cookie,
)
from network_policy import direct_firefox_options
from settings_store import SettingsStore, default_database_path

PROJECT_DIR = Path(__file__).parent
SETTINGS_DB = default_database_path(PROJECT_DIR / "config.yaml")
_settings = SettingsStore(SETTINGS_DB)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("get_cookie")


def interactive_login(
    profile_dir: Path,
    validate: bool = True,
    *,
    include_user_agent: bool = False,
) -> tuple[str | None, str] | tuple[str | None, str, str | None]:
    """Launch visible Firefox, wait for user to log in, extract cookies.

    The profile persists, enabling future headless runs.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        result = (None, (
            "Playwright 未安装。请运行：\n"
            "  uv add playwright\n"
            "  playwright install firefox"
        ))
        return (*result, None) if include_user_agent else result

    profile_dir.mkdir(parents=True, exist_ok=True)

    log.info("正在启动 Firefox (持久化配置: %s)...", profile_dir)
    log.info("提示：本窗口关闭或按 Enter 后，Firefox 会自动关闭。")

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch_persistent_context(
                **direct_firefox_options(),
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

            user_agent = None
            try:
                user_agent = str(page.evaluate("() => navigator.userAgent") or "") or None
            except Exception:
                pass
            cookies = browser.cookies()
            browser.close()

            douyin_cookies = collect_douyin_cookies(cookies)
            if not douyin_cookies:
                result = (None, "未找到抖音 cookie，请确认已成功登录。")
                return (*result, user_agent) if include_user_agent else result

            cookie_str = "; ".join(douyin_cookies)
            log.info("提取到 %d 个抖音 cookie", len(douyin_cookies))

            if validate:
                print(f"{Fore.CYAN}正在验证 cookie...")
                valid, reason = validate_cookie(cookie_str, user_agent=user_agent)
                if not valid:
                    result = (None, f"Cookie 无效：{reason}")
                    return (*result, user_agent) if include_user_agent else result
                print(f"{Fore.GREEN}验证通过: {reason}")

            message = f"交互式登录成功（{len(cookie_str)} 字符）"
            return (cookie_str, message, user_agent) if include_user_agent else (cookie_str, message)

    except Exception as exc:
        message = f"浏览器启动失败: {exc}"
        return (None, message, None) if include_user_agent else (None, message)


def _save_cookie(cookie: str, user_agent: str | None) -> None:
    """Persist a validated cookie in the shared managed settings store."""
    if not user_agent:
        raise ValueError("Firefox User-Agent unavailable")
    _settings.apply_douyin_identity(cookie, user_agent)


def main():
    parser = argparse.ArgumentParser(
        description="获取抖音 cookie 并保存到运行时设置",
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
        cookie, user_agent, msg = extract_cookies_with_user_agent(
            profile_dir=profile_dir,
            headless=True,
            validate=validate,
        )
    else:
        print(f"{Fore.CYAN}交互模式：启动 Firefox 进行登录...")
        cookie, msg, user_agent = interactive_login(
            profile_dir, validate=validate, include_user_agent=True
        )

    if cookie:
        try:
            _save_cookie(cookie, user_agent)
        except Exception:
            # Keep storage errors generic: a database exception must not echo
            # the cookie value or any other sensitive setting.
            print(f"{Fore.RED}X Cookie 保存失败，请检查运行时设置后重试")
            sys.exit(1)
        print()
        print(f"{Fore.GREEN}{Style.BRIGHT}[DONE] Cookie 已保存到运行时设置 ({len(cookie)} 字符)")
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
