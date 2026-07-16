"""Cookie extractor using Playwright + headless Firefox.

Provides a persistent Firefox profile so login state survives across runs.
After the first interactive login, subsequent headless runs can extract
cookies without manual intervention.

Usage:
    from cookie_extractor import extract_cookies, validate_cookie

    cookie, msg = extract_cookies(headless=True)
"""

import base64
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
    "sessionid", "sessionid_ss", "sid_guard", "uid", "LOGIN_STATUS",
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


# ── QR code screenshot (for web login service) ─────────────────────

def screenshot_qr_code(
    profile_dir: Path,
    timeout: int = 30000,
) -> tuple[Optional[str], str]:
    """Launch headless Firefox, navigate to douyin.com, screenshot the QR login element.

    Args:
        profile_dir: Firefox profile directory.
        timeout: Navigation timeout in milliseconds.

    Returns:
        (base64_data_uri_or_None, status_message).
        The base64 string is a ``data:image/png;base64,...`` URI ready for <img src>.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright 未安装"

    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                viewport={"width": 1280, "height": 720},
            )
            page = browser.pages[0] if browser.pages else browser.new_page()

            try:
                page.goto(
                    DOUYIN_HOMEPAGE,
                    wait_until="domcontentloaded",
                    timeout=timeout,
                )
            except Exception:
                pass  # page may have loaded enough for the QR to render

            page.wait_for_timeout(3000)  # let JS render the login modal

            # The homepage does not open the login dialog automatically.  The
            # old implementation searched the feed immediately and therefore
            # fell back to returning a screenshot of the first video grid.
            login_opened = False
            login_selectors = [
                "button:has-text('登录')",
                "[role='button']:has-text('登录')",
            ]
            for sel in login_selectors:
                try:
                    login_button = page.locator(sel).first
                    if login_button.is_visible(timeout=2000):
                        login_button.click(force=True)
                        login_opened = True
                        logger.debug("Opened Douyin login dialog via selector: %s", sel)
                        break
                except Exception:
                    continue

            if not login_opened:
                browser.close()
                return None, "未找到抖音网页的登录按钮，请刷新后重试"

            page.wait_for_timeout(4000)  # let the login QR render

            # Return the complete viewport.  Douyin frequently changes the
            # login modal's obfuscated classes, making element guessing prone
            # to selecting square promotional cards instead of the QR code.
            screenshot_bytes = page.screenshot(type="png", full_page=False)
            logger.debug("Captured complete Douyin login viewport")

            browser.close()

        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}", "完整登录页面已截取"

    except Exception as exc:
        logger.error("QR screenshot failed: %s", exc)
        return None, f"浏览器错误: {exc}"


def check_auth_cookies(profile_dir: Path) -> dict:
    """Check whether the Firefox profile has valid Douyin auth cookies.

    Launches a headless browser, reads cookies from the persistent profile,
    and checks whether the user has logged in (QR code scanned in the Douyin app).

    Args:
        profile_dir: Firefox profile directory.

    Returns:
        Dict with keys:
            status: "logged_in" | "expired" | "pending" | "error"
            cookie_str: str | None  — semicolon-joined cookies (if logged_in)
            auth_count: int  — number of recognised auth tokens found
            message: str  — human-readable status
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "status": "error", "cookie_str": None, "auth_count": 0,
            "message": "Playwright 未安装",
        }

    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                viewport={"width": 1280, "height": 720},
            )
            page = browser.pages[0] if browser.pages else browser.new_page()

            try:
                page.goto(
                    DOUYIN_HOMEPAGE,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception:
                pass

            # Check for QR expiry text on the page
            page_text = ""
            try:
                page_text = page.inner_text("body")
            except Exception:
                pass

            qr_expired = any(
                kw in page_text
                for kw in ["二维码已过期", "点击刷新", "请重新扫码"]
            )

            all_cookies = browser.cookies()
            cookies_dict = {
                c["name"]: c["value"]
                for c in all_cookies
                if c.get("domain", "") in DOUYIN_DOMAINS
            }

            auth_found = [
                name for name in _AUTH_COOKIE_NAMES if name in cookies_dict
            ]

            browser.close()

        if len(auth_found) >= 2:
            cookie_str = "; ".join(
                f"{k}={v}" for k, v in cookies_dict.items()
            )
            return {
                "status": "logged_in",
                "cookie_str": cookie_str,
                "auth_count": len(auth_found),
                "message": f"检测到登录态: {', '.join(auth_found[:3])}",
            }

        if qr_expired:
            return {
                "status": "expired",
                "cookie_str": None,
                "auth_count": 0,
                "message": "二维码已过期，请刷新",
            }

        return {
            "status": "pending",
            "cookie_str": None,
            "auth_count": len(auth_found),
            "message": (
                f"等待扫码... ({len(auth_found)} 个认证 token，需要 ≥2)"
                if auth_found else "等待扫码..."
            ),
        }

    except Exception as exc:
        logger.error("Auth cookie check failed: %s", exc)
        return {
            "status": "error", "cookie_str": None, "auth_count": 0,
            "message": f"浏览器错误: {exc}",
        }
