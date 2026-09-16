"""Playwright-based Douyin metadata fetcher.

Instead of reverse-engineering the a_bogus signature algorithm (which changes
frequently), this module uses a real Firefox browser to make API requests.
The browser's own JavaScript context automatically generates correct a_bogus
signatures for every request.

Architecture:
    A dedicated background event loop owns the Playwright browser instance.
    All callers submit work to this loop via ``asyncio.run_coroutine_threadsafe``,
    serialised by a threading lock so **exactly one** request uses the browser
    at any time.  This avoids cross-event-loop issues (each ``asyncio.run()``
    in the download workers creates a fresh loop) and prevents concurrent
    page navigation / evaluate races.

    A separate temporary profile directory avoids lock conflicts with the
    cookie-extraction Firefox that uses ``~/.douyin_email_bot/firefox_profile``.
    Cookies are injected via the Playwright context API on every page reuse.
"""

import asyncio
import logging
import tempfile
import threading
import time
from typing import Any

from network_policy import direct_firefox_options

logger = logging.getLogger("PlaywrightDouyinFetcher")

DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# ── Background event loop (owns the Playwright browser) ──────────────
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_started = threading.Event()

# Serialise all browser access across caller threads
_browser_lock = threading.Lock()

# Browser state — only touched from within _bg_loop coroutines
_browser = None
_browser_context = None
_browser_page = None
_last_navigation = 0.0
_NAVIGATION_COOLDOWN = 5.0  # seconds between navigations


def _ensure_bg_loop() -> asyncio.AbstractEventLoop:
    """Start the background event loop thread if not already running."""
    global _bg_loop, _bg_thread
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    _bg_loop = asyncio.new_event_loop()
    _bg_thread = threading.Thread(
        target=_bg_loop.run_forever, name="pw-fetcher-loop", daemon=True,
    )
    _bg_thread.start()
    _bg_started.wait(timeout=5)
    return _bg_loop


def _bg_loop_starter(loop: asyncio.AbstractEventLoop):
    """Entry point for the background thread — signal when the loop is live."""
    _bg_started.set()
    loop.run_forever()


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


# ── Internal async helpers (run on _bg_loop) ─────────────────────────


async def _ensure_page(cookie: str, user_agent: str | None = None):
    """Ensure we have a live browser page with Douyin cookies loaded.

    Must only be called from within ``_bg_loop``.
    """
    global _browser, _browser_context, _browser_page, _last_navigation

    from playwright.async_api import async_playwright

    # If page exists and is still alive, reuse it
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
    if cookie:
        try:
            await _set_cookies_from_string(_browser_context, cookie)
        except Exception:
            logger.debug("Failed to set cookies on browser context", exc_info=True)

    # Navigate to Douyin to ensure JS context is established.
    # Use ``networkidle`` because Douyin's homepage redirects via JS
    # (e.g. ``/`` -> ``/jingxuan``), destroying the execution context
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


async def _fetch_aweme_detail_impl(
    aweme_id: str,
    cookie: str,
    user_agent: str | None = None,
) -> dict[str, Any] | None:
    """Internal implementation — runs on ``_bg_loop``."""
    page = await _ensure_page(cookie, user_agent)

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

    result = await page.evaluate(js_code, api_url)

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


# ── Public API (thread-safe, called from download workers) ───────────


def fetch_aweme_detail(
    aweme_id: str,
    cookie: str,
    user_agent: str | None = None,
    timeout: int = 20000,
) -> dict[str, Any] | None:
    """Fetch video metadata using the browser's own fetch().

    Thread-safe: serialised by ``_browser_lock`` so only one request uses
    the browser at a time.  Runs the coroutine on the dedicated background
    event loop to avoid cross-loop issues with ``asyncio.run()``.

    Returns:
        The parsed JSON response dict, or None on failure.
    """
    loop = _ensure_bg_loop()
    with _browser_lock:
        future = asyncio.run_coroutine_threadsafe(
            _fetch_aweme_detail_impl(aweme_id, cookie, user_agent), loop,
        )
        try:
            return future.result(timeout=timeout)
        except Exception as exc:
            logger.error("Playwright fetch failed for aweme_id=%s: %s", aweme_id, exc)
            return None


def shutdown():
    """Gracefully close the persistent browser and stop the background loop."""
    global _browser, _browser_context, _browser_page
    loop = _bg_loop
    if loop is None or not loop.is_running():
        return

    async def _close():
        global _browser, _browser_context, _browser_page
        if _browser_context is not None:
            try:
                await _browser_context.close()
            except Exception:
                pass
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
        _browser_page = None
        _browser_context = None
        _browser = None

    with _browser_lock:
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass
    loop.call_soon_threadsafe(loop.stop)
