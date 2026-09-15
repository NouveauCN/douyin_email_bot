"""Playwright-based Douyin metadata fetcher.

Instead of reverse-engineering the a_bogus signature algorithm (which changes
frequently), this module uses a real Firefox browser to make API requests.
The browser's own JavaScript context automatically generates correct a_bogus
signatures for every request.

Architecture:
    A persistent Playwright Firefox instance stays alive across requests.
    On each fetch, we navigate to a Douyin page (if needed to establish the
    JS context) and use ``page.evaluate()`` to call ``fetch()`` from within
    the browser.  The browser's interceptor adds correct a_bogus, and we
    return the JSON response.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Any, Optional

from cookie_extractor import collect_douyin_cookies
from network_policy import direct_firefox_options

logger = logging.getLogger("PlaywrightDouyinFetcher")

DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_browser_lock = threading.Lock()
_browser_context = None
_browser_page = None
_last_navigation = 0.0
_NAVIGATION_COOLDOWN = 5.0  # seconds between navigations


def _profile_dir() -> str:
    from pathlib import Path
    return str(Path.home() / ".douyin_email_bot" / "firefox_profile")


async def _ensure_page(cookie: str, user_agent: str | None = None):
    """Ensure we have a live browser page with Douyin cookies loaded."""
    global _browser_context, _browser_page, _last_navigation

    from playwright.async_api import async_playwright

    # If page exists and is still alive, just refresh cookies if needed
    if _browser_page is not None:
        try:
            # Quick liveness check
            _ = _browser_page.url
            # If we navigated recently, skip cookie refresh
            if time.time() - _last_navigation < _NAVIGATION_COOLDOWN:
                return _browser_page
        except Exception:
            _browser_page = None
            _browser_context = None

    if _browser_context is None:
        pw = await async_playwright().__aenter__()
        _browser_context = await pw.firefox.launch_persistent_context(
            **direct_firefox_options(),
            user_data_dir=_profile_dir(),
            headless=True,
            viewport={"width": 1280, "height": 720},
        )

    page = _browser_page
    if page is None or page.is_closed():
        page = _browser_context.pages[0] if _browser_context.pages else await _browser_context.new_page()
        _browser_page = page

    # Set cookies from the managed settings into the browser context
    if cookie:
        try:
            await _set_cookies_from_string(_browser_context, cookie)
        except Exception:
            logger.debug("Failed to set cookies on browser context", exc_info=True)

    # Navigate to Douyin to ensure JS context is established
    now = time.time()
    if now - _last_navigation >= _NAVIGATION_COOLDOWN:
        try:
            await page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=15000)
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
    global _browser_context, _browser_page
    if _browser_page is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_browser_context.close())
            else:
                loop.run_until_complete(_browser_context.close())
        except Exception:
            pass
        _browser_page = None
        _browser_context = None
