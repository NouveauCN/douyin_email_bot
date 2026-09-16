"""Playwright-based Douyin metadata fetcher.

Instead of reverse-engineering the a_bogus signature algorithm (which changes
frequently), this module uses a real Firefox browser to make API requests.
The browser's own JavaScript context automatically generates correct a_bogus
signatures for every request.

Architecture:
    A persistent Playwright Firefox instance stays alive across requests,
    using a **separate temporary profile** so it never conflicts with the
    cookie-extraction Firefox.  Cookies are injected via the Playwright
    context API on every page reuse, keeping the profile clean.

    On each fetch, we navigate to a Douyin page (if needed to establish the
    JS context) and use ``page.evaluate()`` to call ``fetch()`` from within
    the browser.  The browser's interceptor adds correct a_bogus, and we
    return the JSON response.
"""

import asyncio
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from network_policy import direct_firefox_options

logger = logging.getLogger("PlaywrightDouyinFetcher")

DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_browser_lock = threading.Lock()
_browser = None
_browser_context = None
_browser_page = None
_last_navigation = 0.0
_NAVIGATION_COOLDOWN = 5.0  # seconds between navigations
_temp_dir: str | None = None


def _get_temp_profile_dir() -> str:
    """Return a dedicated temp directory for the fetcher's Firefox profile.

    Created once per process lifetime so the browser can reuse it across
    requests.  This avoids the profile-lock conflict with the cookie-
    extraction Firefox that uses ``~/.douyin_email_bot/firefox_profile``.
    """
    global _temp_dir
    if _temp_dir is None:
        _temp_dir = tempfile.mkdtemp(prefix="pw_fetcher_")
    return _temp_dir


def _firefox_launch_env() -> dict:
    """Environment for the fetcher's Firefox.

    ``MOZ_DISABLE_SANDBOX`` and ``MOZ_DISABLE_CONTENT_SANDBOX`` are set to 1
    so Firefox can launch when the kernel refuses ``clone()`` for user
    namespaces (``EPERM`` in Docker without ``--privileged`` or ``SYS_ADMIN``).
    """
    env = dict(direct_firefox_options().get("env", {}))
    env["MOZ_DISABLE_SANDBOX"] = "1"
    env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
    return env


async def _ensure_page(cookie: str, user_agent: str | None = None):
    """Ensure we have a live browser page with Douyin cookies loaded."""
    global _browser, _browser_context, _browser_page, _last_navigation

    from playwright.async_api import async_playwright

    # If page exists and is still alive, just refresh cookies if needed
    if _browser_page is not None:
        try:
            _ = _browser_page.url  # liveness check
            if time.time() - _last_navigation < _NAVIGATION_COOLDOWN:
                return _browser_page
        except Exception:
            _browser_page = None
            _browser_context = None
            _browser = None

    if _browser is None:
        pw_instance = await async_playwright().__aenter__()
        _browser = await pw_instance.firefox.launch(
            headless=True,
            env=_firefox_launch_env(),
        )

    if _browser_context is None:
        ctx_opts: dict[str, Any] = {"viewport": {"width": 1280, "height": 720}}
        if user_agent:
            ctx_opts["user_agent"] = user_agent
        _browser_context = await _browser.new_context(**ctx_opts)

    page = _browser_page
    if page is None or page.is_closed():
        page = _browser_context.pages[0] if _browser_context.pages else await _browser_context.new_page()
        _browser_page = page

    # Inject cookies from the managed cookie string into the browser context.
    # This replaces any stale cookies on every page reuse cycle.
    if cookie:
        try:
            await _set_cookies_from_string(_browser_context, cookie)
        except Exception:
            logger.debug("Failed to set cookies on browser context", exc_info=True)

    # Navigate to Douyin to ensure JS context is established.
    # Use ``networkidle`` because Douyin's homepage redirects via JS
    # (e.g. ``/`` -> ``/jingxuan``), which destroys the execution context
    # if we only wait for ``domcontentloaded``.
    now = time.time()
    if now - _last_navigation >= _NAVIGATION_COOLDOWN:
        try:
            await page.goto(DOUYIN_HOME, wait_until="networkidle", timeout=30000)
        except Exception as exc:
            logger.debug("Navigation warning (may be acceptable): %s", exc)
        _last_navigation = time.time()

    return page


async def _set_cookies_from_string(context, cookie_str: str):
    """Parse a cookie string and set cookies on the browser context."""
    cookies = []
    for part in cookie_str.split("; "):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".douyin.com",
            "path": "/",
        })
    if cookies:
        await context.add_cookies(cookies)


async def fetch_aweme_detail(
    aweme_id: str,
    cookie: str,
    user_agent: str | None = None,
    timeout: int = 20000,
) -> dict[str, Any] | None:
    """Fetch video metadata using the browser's own fetch().

    The browser executes ``fetch()`` in the Douyin page context, so its
    JavaScript interceptor adds the correct a_bogus and other anti-bot
    parameters automatically.

    Returns:
        The parsed JSON response dict, or None on failure.
    """
    page = await _ensure_page(cookie, user_agent)

    # Build the API URL with query parameters (same as F2 would)
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Linux x86_64",
        "browser_name": "Firefox",
        "browser_version": "153.0",
        "browser_online": "true",
        "engine_name": "Gecko",
        "engine_version": "153.0",
        "os_name": "Linux",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "100",
        "aweme_id": aweme_id,
    }

    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    api_url = f"{DOUYIN_DETAIL_API}?{query_string}"

    # Use the browser's fetch() — the page's JS interceptor adds a_bogus
    js_code = """
    async (url) => {
        try {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'Referer': 'https://www.douyin.com/',
                },
            });
            if (!resp.ok) {
                return { __error: true, __status: resp.status, __statusText: resp.statusText };
            }
            return await resp.json();
        } catch (e) {
            return { __error: true, __message: e.message || String(e) };
        }
    }
    """

    try:
        result = await page.evaluate(js_code, api_url)
    except Exception as exc:
        logger.error("Playwright fetch failed for aweme_id=%s: %s", aweme_id, exc)
        return None

    if result is None:
        logger.error("Playwright fetch returned None for aweme_id=%s", aweme_id)
        return None

    if isinstance(result, dict) and result.get("__error"):
        status = result.get("__status")
        msg = result.get("__message") or result.get("__statusText", "")
        logger.error(
            "Playwright fetch error for aweme_id=%s: status=%s message=%s",
            aweme_id, status, msg,
        )
        return None

    return result


def shutdown():
    """Gracefully close the persistent browser."""
    global _browser_context, _browser_page, _browser
    try:
        loop = asyncio.get_event_loop()
        async def _close():
            if _browser_page is not None:
                try:
                    await _browser_context.close()
                except Exception:
                    pass
            if _browser is not None:
                try:
                    await _browser.close()
                except Exception:
                    pass
        if loop.is_running():
            asyncio.ensure_future(_close())
        else:
            loop.run_until_complete(_close())
    except Exception:
        pass
    _browser_page = None
    _browser_context = None
    _browser = None
