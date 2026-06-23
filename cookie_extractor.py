"""Cookie extractor using Playwright + headless Firefox.

Provides a persistent Firefox profile so login state survives across runs.
After the first interactive login, subsequent headless runs can extract
cookies without manual intervention.

Usage:
    from cookie_extractor import extract_cookies, validate_cookie

    cookie, msg = extract_cookies(headless=True)
"""

import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("CookieExtractor")

DEFAULT_PROFILE_DIR = Path.home() / ".douyin_email_bot" / "firefox_profile"
DOUYIN_DOMAINS = frozenset({".douyin.com", "douyin.com", "www.douyin.com"})
DOUYIN_HOMEPAGE = "https://www.douyin.com/"

# Auth-indicating cookie names — if present, the session is logged in.
_AUTH_COOKIE_NAMES = frozenset({
    "sessionid", "sessionid_ss", "sid_guard", "uid", "passport_csrf_token",
    "passport_csrf_token_default", "odin_tt", "LOGIN_STATUS",
})


# ── Extraction ────────────────────────────────────────────────────

def extract_with_playwright(
    profile_dir: Path,
    headless: bool = True,
    timeout: int = 30000,
) -> Optional[str]:
    """Launch Firefox via Playwright, navigate to douyin.com, extract cookies.

    Uses a persistent browser context so login state survives across runs.

    Args:
        profile_dir: Firefox profile directory (created if missing).
        headless: True = no GUI, False = visible browser for interactive login.
        timeout: Navigation timeout in milliseconds.

    Returns:
        Semicolon-joined cookie string, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        logger.error(
            "Playwright not installed. Run:\n"
            "  uv add playwright\n"
            "  playwright install firefox"
        )
        return None

    profile_dir.mkdir(parents=True, exist_ok=True)

    # For headless mode, try two sets of args: first the standard, then
    # with --headless=new which can be more reliable on some systems.
    headless_args_attempts = [
        (headless, []),
    ]
    if headless:
        headless_args_attempts.append((True, ["--headless=new"]))

    last_error = None

    for attempt, (use_headless, extra_args) in enumerate(headless_args_attempts):
        try:
            with sync_playwright() as p:
                launch_kwargs: dict = {
                    "user_data_dir": str(profile_dir),
                    "headless": use_headless,
                    "viewport": {"width": 1280, "height": 720},
                }
                if extra_args:
                    launch_kwargs["args"] = extra_args

                browser = p.firefox.launch_persistent_context(**launch_kwargs)

                page = browser.pages[0] if browser.pages else browser.new_page()

                try:
                    page.goto(
                        DOUYIN_HOMEPAGE,
                        wait_until="domcontentloaded",
                        timeout=timeout,
                    )
                except Exception as exc:
                    logger.debug("Page navigation warning: %s", exc)

                all_cookies = browser.cookies()
                browser.close()

                douyin_cookies = [
                    f"{c['name']}={c['value']}"
                    for c in all_cookies
                    if c.get("domain", "") in DOUYIN_DOMAINS
                ]

                if not douyin_cookies:
                    logger.warning("No douyin.com cookies in browser context")
                    return None

                logger.info(
                    "Extracted %d douyin cookies (%d chars) via Playwright%s",
                    len(douyin_cookies),
                    sum(len(c) for c in douyin_cookies) + len(douyin_cookies) - 1,
                    " (--headless=new)" if extra_args else "",
                )
                return "; ".join(douyin_cookies)

        except Exception as exc:
            last_error = exc
            if attempt < len(headless_args_attempts) - 1:
                logger.debug(
                    "Headless attempt %d failed, retrying with different args: %s",
                    attempt + 1, exc,
                )
                continue

    logger.error("Playwright extraction failed: %s", last_error)
    return None


# ── Cookie quality ────────────────────────────────────────────────

def _parse_cookie_dict(cookie_str: str) -> dict[str, str]:
    """Parse a cookie string into {name: value} dict."""
    result = {}
    for item in cookie_str.split("; "):
        if "=" in item:
            key, _, val = item.partition("=")
            result[key] = val
    return result


def _assess_quality(cookie_str: str) -> tuple[str, bool]:
    """Assess cookie quality. Returns (grade_label, is_authenticated).

    Grades: "已登录" (authenticated), "匿名会话" (anonymous), "基础" (minimal).
    """
    cookies = _parse_cookie_dict(cookie_str)
    auth_found = [name for name in _AUTH_COOKIE_NAMES if name in cookies]

    if len(auth_found) >= 3:
        return f"已登录 (包含 {', '.join(auth_found[:3])})", True
    elif len(auth_found) >= 1:
        return f"已登录 (含 {auth_found[0]})", True
    elif len(cookies) >= 10:
        return "匿名会话 (cookie 较多但无登录标识)", False
    else:
        return "基础会话 (cookie 较少)", False


# ── Validation ─────────────────────────────────────────────────────

def validate_cookie(cookie_str: str, timeout: int = 15) -> tuple[bool, str]:
    """Test a cookie against douyin.com to check if it's still valid.

    Args:
        cookie_str: Semicolon-joined cookie string.
        timeout: HTTP request timeout in seconds.

    Returns:
        (is_valid, reason).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie_str,
        "Referer": "https://www.douyin.com/",
    }

    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            resp = client.get(DOUYIN_HOMEPAGE)

        final_url = str(resp.url)
        if "login" in final_url.lower():
            return False, "Cookie 已过期 (重定向至登录页)"

        if resp.status_code in (401, 403):
            return False, f"Cookie 被拒绝 (HTTP {resp.status_code})"

        return True, "Cookie 有效"

    except httpx.TimeoutException:
        return True, "验证未完成 (超时)"
    except httpx.HTTPError as exc:
        logger.debug("Validation HTTP error: %s", exc)
        return True, "验证未完成 (网络错误)"
    except Exception as exc:
        logger.debug("Validation unexpected error: %s", exc)
        return True, "验证未完成"


# ── Orchestrator ───────────────────────────────────────────────────

def extract_cookies(
    profile_dir: Optional[Path] = None,
    headless: bool = True,
    validate: bool = True,
) -> tuple[Optional[str], str]:
    """Extract douyin.com cookies, optionally validate.

    This is the main entry point. Launches Firefox via Playwright
    and extracts cookies from the browser context.

    Args:
        profile_dir: Firefox profile directory (default: ~/.douyin_email_bot/firefox_profile).
        headless: True for headless, False for interactive login.
        validate: If True, test cookie validity against douyin.com.

    Returns:
        (cookie_string_or_None, status_message).
    """
    if profile_dir is None:
        profile_dir = DEFAULT_PROFILE_DIR

    logger.info(
        "Extracting cookies (headless=%s, profile=%s)",
        headless, profile_dir,
    )

    cookie_str = extract_with_playwright(profile_dir, headless=headless)

    if cookie_str is None:
        if headless and not _has_login_state(profile_dir):
            return None, (
                "未找到抖音 cookie (无登录状态)。\n"
                "首次使用需先交互式登录：uv run python get_cookie.py"
            )
        return None, "未找到抖音 cookie，请确认已登录。"

    # ── Assess quality ──
    grade, is_auth = _assess_quality(cookie_str)

    if not validate:
        return cookie_str, f"Cookie 已提取 ({len(cookie_str)} 字符) — {grade}"

    valid, reason = validate_cookie(cookie_str)
    if valid:
        return cookie_str, f"Cookie 有效 ({len(cookie_str)} 字符) — {grade}"
    else:
        logger.warning("Extracted cookie is invalid: %s", reason)
        return None, (
            f"Cookie 已过期或无效：{reason}\n"
            "请运行 uv run python get_cookie.py 重新登录。"
        )


def _has_login_state(profile_dir: Path) -> bool:
    """Check if the profile directory has signs of prior browser use."""
    return (profile_dir / "cookies.sqlite").exists()
