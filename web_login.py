"""Web-based Douyin QR code login service.

Replaces get_cookie.py's interactive login (which blocks on input()) with
a web interface: open http://<host>:8080, scan the QR code with the Douyin
app, and cookies are automatically saved to the managed runtime settings store.

Usage:
    python web_login.py [--port 8080] [--host 127.0.0.1]
"""

import argparse
import base64
from collections import deque
import hashlib
import hmac
import logging
import math
import os
import queue
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template_string, request, session

# ── Bootstrap: same as main.py (needed before any F2 imports) ─────
_PROJECT_DIR = Path(__file__).parent

# Load config for profile_dir
from config_loader import load_config  # noqa: E402
from f2_bootstrap import bootstrap_f2  # noqa: E402
from settings_store import SettingsStore, default_database_path, read_dotenv  # noqa: E402

bootstrap_f2(_PROJECT_DIR)

_config = load_config(_PROJECT_DIR / "config.yaml")
_settings = SettingsStore(default_database_path(_PROJECT_DIR / "config.yaml"))

from cookie_extractor import (  # noqa: E402
    DOUYIN_HOMEPAGE,
    _AUTH_COOKIE_NAMES,
    _assess_quality,
    check_auth_cookies,
    screenshot_qr_code,
)

# ── App setup ─────────────────────────────────────────────────────

app = Flask(__name__)
log = logging.getLogger("web_login")
_browser_lock = threading.Lock()
_STATUS_RESPONSE_FIELDS = ("status", "auth_count", "message")
_RATE_LIMIT_DEFAULTS = {"unlock": 5, "qr": 5, "status": 120, "desktop": 240}
_RATE_LIMIT_ENV_VARS = {
    "unlock": "WEB_LOGIN_PASSWORD_RATE_LIMIT",
    "qr": "WEB_LOGIN_QR_RATE_LIMIT",
    "status": "WEB_LOGIN_STATUS_RATE_LIMIT",
    "desktop": "WEB_LOGIN_DESKTOP_RATE_LIMIT",
}
_DEFAULT_RATE_WINDOW_SECONDS = 60
_SESSION_SECONDS = 15 * 60
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], deque[float]] = {}
_DESKTOP_WIDTH = 1280
_DESKTOP_HEIGHT = 720
_MAX_DESKTOP_JSON_BYTES = 16 * 1024


class RemoteBrowserError(RuntimeError):
    """A safe, user-facing browser-controller error."""


class RemoteBrowserSession:
    """One server-owned, password-gated Playwright Firefox session.

    The context is deliberately kept alive between requests.  Every browser
    operation is serialized with ``_browser_lock`` because Firefox profiles
    must never be opened concurrently by the QR/status fallback or by another
    remote session.
    """

    def __init__(self):
        self._context = None
        self._playwright = None
        self._owner: str | None = None
        self._thread = None
        self._commands = None
        self._width = _DESKTOP_WIDTH
        self._height = _DESKTOP_HEIGHT

    def active_for(self, owner: str | None) -> bool:
        return bool(self._context is not None and owner and self._owner == owner)

    def active(self) -> bool:
        return self._context is not None

    def start(self, owner: str) -> tuple[bool, str]:
        if not owner:
            raise RemoteBrowserError("远程桌面会话无效")
        with _browser_lock:
            if self._context is not None:
                if self._owner != owner:
                    return False, "已有远程桌面会话正在使用，请先锁定原会话"
                return True, "远程桌面已连接"
            ready = threading.Event()
            result: dict = {}
            self._commands = queue.Queue()
            self._thread = threading.Thread(
                target=self._browser_worker,
                args=(owner, ready, result, time.monotonic() + _SESSION_SECONDS),
                name="web-login-firefox",
                daemon=True,
            )
            self._thread.start()
            ready.wait(35)
            if not result.get("ok"):
                self._thread = None
                self._commands = None
                raise RemoteBrowserError("浏览器启动失败，请稍后重试")
            return True, "远程桌面已连接"

    def _browser_worker(
        self,
        owner: str,
        ready: threading.Event,
        result: dict,
        deadline: float,
    ):
        """Own the sync Playwright objects on one stable thread."""
        context = playwright = None
        try:
            from playwright.sync_api import sync_playwright

            profile_dir = _get_profile_dir()
            profile_dir.mkdir(parents=True, exist_ok=True)
            playwright = sync_playwright().start()
            context = playwright.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                viewport={"width": self._width, "height": self._height},
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(DOUYIN_HOMEPAGE, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            self._playwright = playwright
            self._context = context
            self._owner = owner
            result["ok"] = True
            ready.set()
            commands = self._commands
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    command = commands.get(timeout=remaining)
                except queue.Empty:
                    break
                if command is None:
                    break
                callback, event, holder = command
                try:
                    holder["value"] = callback()
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    event.set()
        except Exception:
            result["ok"] = False
            ready.set()
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass
            self._context = None
            self._playwright = None
            self._owner = None

    def _call(self, callback):
        if self._thread is threading.current_thread():
            return callback()
        if self._commands is None or self._thread is None:
            raise RemoteBrowserError("远程桌面未启动或已被锁定")
        event, holder = threading.Event(), {}
        self._commands.put((callback, event, holder))
        if not event.wait(35):
            raise RemoteBrowserError("浏览器响应超时，请重试")
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    def stop(self, owner: str | None = None) -> bool:
        with _browser_lock:
            if self._context is None:
                return False
            if owner is not None and owner != self._owner:
                return False
            thread, commands = self._thread, self._commands
            self._context = None
            self._playwright = None
            self._owner = None
            self._thread = None
            self._commands = None
            if commands is not None:
                commands.put(None)
            if thread is not None and thread is not threading.current_thread():
                thread.join(35)
            return True

    def _page_locked(self, owner: str):
        if self._context is None or self._owner != owner:
            raise RemoteBrowserError("远程桌面未启动或已被锁定")
        pages = self._context.pages
        if not pages:
            return self._context.new_page()
        return pages[0]

    def frame(self, owner: str) -> tuple[str, int, int]:
        with _browser_lock:
            def capture():
                page = self._page_locked(owner)
                try:
                    return page.screenshot(type="jpeg", quality=65, full_page=False)
                except Exception:
                    raise RemoteBrowserError("截图失败，请刷新远程桌面")
            screenshot = self._call(capture)
            encoded = base64.b64encode(screenshot).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}", self._width, self._height

    def input(self, owner: str, event: dict) -> None:
        with _browser_lock:
            def send():
                page = self._page_locked(owner)
                kind = event.get("kind")
                try:
                    if kind == "click":
                        page.mouse.click(event["x"], event["y"], button=event["button"], click_count=event["click_count"])
                    elif kind == "wheel":
                        page.mouse.wheel(event["delta_x"], event["delta_y"])
                    elif kind == "key":
                        page.keyboard.press(event["key"])
                    elif kind == "text":
                        page.keyboard.insert_text(event["text"])
                    else:
                        raise RemoteBrowserError("不支持的输入类型")
                except RemoteBrowserError:
                    raise
                except Exception:
                    raise RemoteBrowserError("输入发送失败，请重试")
            self._call(send)

    def resize(self, owner: str, width: int, height: int) -> None:
        with _browser_lock:
            def resize_page():
                page = self._page_locked(owner)
                try:
                    page.set_viewport_size({"width": width, "height": height})
                except Exception:
                    raise RemoteBrowserError("调整远程桌面大小失败")
            self._call(resize_page)
            self._width, self._height = width, height

    def reload(self, owner: str) -> None:
        with _browser_lock:
            def reload_page():
                page = self._page_locked(owner)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            self._call(reload_page)

    def cookies(self, owner: str) -> tuple[str | None, int]:
        with _browser_lock:
            def read_cookies():
                if self._context is None or self._owner != owner:
                    raise RemoteBrowserError("远程桌面未启动或已被锁定")
                try:
                    return self._context.cookies()
                except Exception:
                    raise RemoteBrowserError("读取登录状态失败，请重试")
            all_cookies = self._call(read_cookies)
            selected = [
                c for c in all_cookies
                if (
                    (domain := str(c.get("domain", "")).lstrip(".").lower())
                    == "douyin.com"
                    or domain.endswith(".douyin.com")
                )
            ]
            cookie_str = "; ".join(
                f"{c.get('name', '')}={c.get('value', '')}" for c in selected
                if c.get("name")
            ) or None
            auth_count = sum(1 for c in selected if c.get("name") in _AUTH_COOKIE_NAMES)
            return cookie_str, auth_count


_remote_browser = RemoteBrowserSession()


def _password() -> str:
    """Read the deployment-owned password without ever logging or returning it."""
    # Compose injects this explicitly for the file browser.  The fallback keeps
    # ``python web_login.py`` useful on a trusted local shell without loading
    # dotenv values into the process environment.
    return os.environ.get("WEB_LOGIN_PASSWORD") or read_dotenv(
        _PROJECT_DIR / ".env"
    ).get("WEB_LOGIN_PASSWORD", "")


def _session_secret(password: str) -> bytes:
    if password:
        return hashlib.sha256(
            b"douyin-email-bot:web-login-session:" + password.encode("utf-8")
        ).digest()
    return secrets.token_bytes(32)


def _configure_app(target_app: Flask) -> None:
    if not target_app.secret_key:
        target_app.secret_key = _session_secret(_password())
    target_app.config["PERMANENT_SESSION_LIFETIME"] = _SESSION_SECONDS
    target_app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    if target_app.config.get("SESSION_COOKIE_SAMESITE") is None:
        target_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    target_app.config.setdefault("SESSION_COOKIE_SECURE", False)


def _get_profile_dir() -> Path:
    raw = _config.cookie_extractor.profile_dir
    if raw:
        return Path(raw)
    return Path.home() / ".douyin_email_bot" / "firefox_profile"


def _canonical_origin(value: str, *, allow_path: bool = False) -> str | None:
    """Return a normalized origin, or None for an invalid URL/header value."""
    if not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and parsed.path not in {"", "/"})
        or (not allow_path and (parsed.query or parsed.fragment))
    ):
        return None

    scheme = parsed.scheme.lower()
    hostname = hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{hostname}{port_suffix}"


def _allowed_origins() -> set[str]:
    """Read the exact, comma-separated origin allowlist from the environment."""
    configured = os.environ.get("WEB_LOGIN_ALLOWED_ORIGINS", "")
    return {
        origin
        for item in configured.split(",")
        if (origin := _canonical_origin(item.strip())) is not None
    }


def _request_origin() -> str | None:
    return _canonical_origin(f"{request.scheme}://{request.host}")


def _has_allowed_source() -> bool:
    """Require Origin, or Referer when Origin is absent, to match this app."""
    origin_header = request.headers.get("Origin")
    if origin_header is not None:
        source = _canonical_origin(origin_header)
    else:
        referer = request.headers.get("Referer")
        source = _canonical_origin(referer, allow_path=True) if referer is not None else None
    if source is None:
        return False
    return source == _request_origin() or source in _allowed_origins()


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _rate_limit_response(kind: str):
    """Return a 429 response when this client has exhausted the endpoint quota."""
    limit = _positive_env_int(_RATE_LIMIT_ENV_VARS[kind], _RATE_LIMIT_DEFAULTS[kind])
    window = _positive_env_int("WEB_LOGIN_RATE_WINDOW_SECONDS", _DEFAULT_RATE_WINDOW_SECONDS)
    client = request.remote_addr or "unknown"
    now = time.monotonic()
    bucket_key = (kind, client)

    with _rate_limit_lock:
        bucket = _rate_limit_buckets.setdefault(bucket_key, deque())
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, math.ceil(window - (now - bucket[0])))
            response = jsonify({"success": False, "message": "请求过于频繁，请稍后重试"})
            response.headers["Cache-Control"] = "no-store"
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
        bucket.append(now)
    return None


def _login_api_kind(path: str) -> str | None:
    for kind in (
        "unlock", "qr", "status", "logout", "desktop/start", "desktop/frame",
        "desktop/input", "desktop/resize", "desktop/reload", "desktop/save",
        "desktop/lock",
    ):
        if path == f"/api/{kind}" or path.endswith(f"/api/web-login/{kind}"):
            return "desktop" if kind.startswith("desktop/") else kind
    return None


def _unauthorized(message="请先验证登录密码"):
    response = jsonify({"success": False, "message": message})
    response.headers["Cache-Control"] = "no-store"
    return response, 401


def _service_unavailable():
    response = jsonify({"success": False, "message": "Web Login 未配置密码"})
    response.headers["Cache-Control"] = "no-store"
    return response, 503


def _session_is_unlocked() -> bool:
    """Validate the short-lived session and tear down expired browser state."""
    if not session.get("web_login_unlocked"):
        return False
    unlocked_at = session.get("web_login_unlocked_at")
    if not isinstance(unlocked_at, (int, float)) or time.time() - unlocked_at > _SESSION_SECONDS:
        owner = session.get("web_login_owner")
        if isinstance(owner, str):
            _remote_browser.stop(owner)
        session.clear()
        return False
    return True


def _protect_login_request():
    kind = _login_api_kind(request.path)
    if kind is None:
        return None
    if not _has_allowed_source():
        response = jsonify({"success": False, "message": "请求来源不被允许"})
        response.headers["Cache-Control"] = "no-store"
        return response, 403
    if kind in {"unlock", "qr", "status", "desktop"} and not _password():
        return _service_unavailable()
    if kind in _RATE_LIMIT_ENV_VARS:
        limited = _rate_limit_response(kind)
        if limited:
            return limited
    if kind in {"qr", "status", "desktop"} and not _session_is_unlocked():
        return _unauthorized()
    return None


_configure_app(app)
app.before_request(_protect_login_request)


# ── Routes ─────────────────────────────────────────────────────────

@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    """Unlock the QR controls for a short-lived browser session."""
    configured = _password()
    if not configured:
        return _service_unavailable()
    payload = request.get_json(silent=True) or {}
    candidate = payload.get("password", "")
    if not isinstance(candidate, str):
        candidate = ""
    if not hmac.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8")):
        return _unauthorized("密码错误")
    session.clear()
    session.permanent = True
    session["web_login_unlocked"] = True
    session["web_login_unlocked_at"] = time.time()
    session["web_login_owner"] = secrets.token_urlsafe(24)
    response = jsonify({"success": True, "message": "验证成功"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Relock the QR controls and invalidate the current session."""
    owner = session.get("web_login_owner")
    _remote_browser.stop(owner if isinstance(owner, str) else None)
    session.clear()
    response = jsonify({"success": True})
    response.headers["Cache-Control"] = "no-store"
    return response


def _desktop_owner() -> str:
    owner = session.get("web_login_owner")
    if not isinstance(owner, str) or not owner:
        raise RemoteBrowserError("远程桌面会话无效")
    return owner


def _desktop_response(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _desktop_failure(exc: RemoteBrowserError, status: int = 409):
    return _desktop_response({"success": False, "message": str(exc)}, status)


def _desktop_payload() -> dict:
    if request.content_length is not None and request.content_length > _MAX_DESKTOP_JSON_BYTES:
        raise RemoteBrowserError("请求过大")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RemoteBrowserError("请求格式无效")
    return payload


def _number(value, *, minimum: float, maximum: float, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RemoteBrowserError("输入参数无效")
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise RemoteBrowserError("输入参数超出范围")
    if integer and int(value) != value:
        raise RemoteBrowserError("输入参数无效")
    return int(value) if integer else float(value)


def _validate_desktop_input(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind == "click":
        button = payload.get("button", "left")
        if button not in {"left", "middle", "right"}:
            raise RemoteBrowserError("鼠标按键无效")
        clicks = _number(payload.get("click_count", 1), minimum=1, maximum=3, integer=True)
        return {
            "kind": kind,
            "x": _number(payload.get("x"), minimum=0, maximum=_DESKTOP_WIDTH),
            "y": _number(payload.get("y"), minimum=0, maximum=_DESKTOP_HEIGHT),
            "button": button,
            "click_count": clicks,
        }
    if kind == "wheel":
        return {
            "kind": kind,
            "delta_x": _number(payload.get("delta_x", 0), minimum=-2000, maximum=2000),
            "delta_y": _number(payload.get("delta_y", 0), minimum=-2000, maximum=2000),
        }
    if kind == "key":
        key = payload.get("key")
        if not isinstance(key, str) or not key or len(key) > 64:
            raise RemoteBrowserError("键盘输入无效")
        return {"kind": kind, "key": key}
    if kind == "text":
        value = payload.get("text")
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise RemoteBrowserError("文本输入无效")
        return {"kind": kind, "text": value}
    raise RemoteBrowserError("不支持的输入类型")


@app.route("/api/desktop/start", methods=["POST"])
def api_desktop_start():
    try:
        owner = _desktop_owner()
        available, message = _remote_browser.start(owner)
        if not available:
            return _desktop_response({"success": False, "message": message}, 409)
        frame, width, height = _remote_browser.frame(owner)
        return _desktop_response({
            "success": True, "frame": frame, "width": width, "height": height,
            "message": message,
        })
    except RemoteBrowserError as exc:
        return _desktop_failure(exc, 503)


@app.route("/api/desktop/frame")
def api_desktop_frame():
    try:
        owner = _desktop_owner()
        frame, width, height = _remote_browser.frame(owner)
        return _desktop_response({"success": True, "frame": frame, "width": width, "height": height})
    except RemoteBrowserError as exc:
        return _desktop_failure(exc)


@app.route("/api/desktop/input", methods=["POST"])
def api_desktop_input():
    try:
        owner = _desktop_owner()
        event = _validate_desktop_input(_desktop_payload())
        _remote_browser.input(owner, event)
        return _desktop_response({"success": True})
    except RemoteBrowserError as exc:
        return _desktop_failure(exc, 400)


@app.route("/api/desktop/resize", methods=["POST"])
def api_desktop_resize():
    try:
        owner = _desktop_owner()
        payload = _desktop_payload()
        width = _number(payload.get("width"), minimum=320, maximum=1920, integer=True)
        height = _number(payload.get("height"), minimum=200, maximum=1200, integer=True)
        _remote_browser.resize(owner, width, height)
        return _desktop_response({"success": True, "width": width, "height": height})
    except RemoteBrowserError as exc:
        return _desktop_failure(exc, 400)


@app.route("/api/desktop/reload", methods=["POST"])
def api_desktop_reload():
    try:
        owner = _desktop_owner()
        _remote_browser.reload(owner)
        frame, width, height = _remote_browser.frame(owner)
        return _desktop_response({"success": True, "frame": frame, "width": width, "height": height})
    except RemoteBrowserError as exc:
        return _desktop_failure(exc)


@app.route("/api/desktop/save", methods=["POST"])
def api_desktop_save():
    try:
        owner = _desktop_owner()
        cookie_str, auth_count = _remote_browser.cookies(owner)
        if not cookie_str:
            return _desktop_response({"success": False, "status": "pending", "auth_count": 0, "message": "尚未检测到抖音登录状态"}, 409)
        grade, is_authenticated = _assess_quality(cookie_str)
        if not is_authenticated:
            return _desktop_response({"success": False, "status": "pending", "auth_count": auth_count, "message": f"尚未检测到完整登录状态（{grade}）"}, 409)
        try:
            _settings.apply([{"key": "douyin.cookie", "action": "set", "value": cookie_str}])
        except Exception:
            log.error("Remote login cookie could not be saved")
            return _desktop_response({"success": False, "message": "Cookie 保存失败，请稍后重试"}, 500)
        log.info("Remote login cookie saved (%d auth tokens)", auth_count)
        return _desktop_response({"success": True, "status": "logged_in", "auth_count": auth_count, "message": f"登录状态已保存（{grade}）"})
    except RemoteBrowserError as exc:
        return _desktop_failure(exc)


@app.route("/api/desktop/lock", methods=["POST"])
def api_desktop_lock():
    owner = session.get("web_login_owner")
    _remote_browser.stop(owner if isinstance(owner, str) else None)
    session.clear()
    return _desktop_response({"success": True, "message": "远程桌面已结束并锁定"})

@app.route("/")
def index():
    """Serve the QR login page."""
    return render_template_string(LOGIN_HTML)


@app.route("/api/qr")
def api_qr():
    """Generate a fresh QR code screenshot."""
    owner = session.get("web_login_owner")
    if isinstance(owner, str) and _remote_browser.active_for(owner):
        try:
            frame, width, height = _remote_browser.frame(owner)
            return _desktop_response({"success": True, "qr_image": frame, "frame": frame, "width": width, "height": height, "message": "完整登录页面已截取"})
        except RemoteBrowserError as exc:
            return _desktop_failure(exc)
    profile_dir = _get_profile_dir()
    with _browser_lock:
        b64, msg = screenshot_qr_code(profile_dir)
    if b64:
        log.info("QR screenshot: %d chars", len(b64))
        response = jsonify({"success": True, "qr_image": b64, "message": msg})
        response.headers["Cache-Control"] = "no-store"
        return response
    log.error("QR screenshot failed: %s", msg)
    response = jsonify({"success": False, "message": msg})
    response.headers["Cache-Control"] = "no-store"
    return response, 500


@app.route("/api/status")
def api_status():
    """Check whether the user has scanned the QR and logged in."""
    owner = session.get("web_login_owner")
    if isinstance(owner, str) and _remote_browser.active_for(owner):
        try:
            cookie_str, auth_count = _remote_browser.cookies(owner)
        except RemoteBrowserError as exc:
            return _desktop_failure(exc)
        if cookie_str:
            grade, is_authenticated = _assess_quality(cookie_str)
            if is_authenticated:
                try:
                    _settings.apply([{"key": "douyin.cookie", "action": "set", "value": cookie_str}])
                except Exception:
                    log.error("Login cookie detected but could not be saved to runtime settings")
                    return _desktop_response({"status": "failure", "auth_count": auth_count, "message": "Cookie 保存失败，请稍后重试"}, 500)
                return _desktop_response({"status": "logged_in", "auth_count": auth_count, "message": f"登录状态已检测并保存（{grade}）"})
        return _desktop_response({"status": "pending", "auth_count": auth_count, "message": "等待扫码或登录"})
    profile_dir = _get_profile_dir()
    with _browser_lock:
        result = check_auth_cookies(profile_dir)

    cookie_str = result.get("cookie_str")
    if result.get("status") == "logged_in" and cookie_str:
        # Validate cookie quality before saving
        grade, _ = _assess_quality(cookie_str)
        result["message"] += f" — {grade}"

        # Persist only after quality validation. Do not put the cookie in the
        # process environment: it would hide later managed-store updates.
        try:
            _settings.apply([{"key": "douyin.cookie", "action": "set", "value": cookie_str}])
        except Exception:
            # Never include the cookie or the storage exception in a response
            # or log line.  The browser can retry after the operator fixes the
            # environment override or runtime database.
            log.error("Login cookie detected but could not be saved to runtime settings")
            response = jsonify({
                "status": "failure",
                "auth_count": result.get("auth_count"),
                "message": "Cookie 保存失败，请稍后重试",
            })
            response.headers["Cache-Control"] = "no-store"
            return response, 500
        log.info(
            "Login success! Cookie saved (%d chars, %d auth tokens) — %s",
            len(cookie_str), result["auth_count"], grade,
        )

    # Cookie contents are used only for persistence and must never reach clients.
    response = jsonify({key: result.get(key) for key in _STATUS_RESPONSE_FIELDS})
    response.headers["Cache-Control"] = "no-store"
    return response


def register_web_login(target_app: Flask, url_prefix: str = "") -> None:
    """Attach the password-gated QR API to another Flask application.

    The file browser embeds these routes under ``/api/web-login`` while this
    module keeps the standalone local CLI fallback at ``/api``.
    """
    _configure_app(target_app)
    target_app.before_request(_protect_login_request)
    prefix = url_prefix.rstrip("/")
    for suffix, view, methods in (
        ("/unlock", api_unlock, ["POST"]),
        ("/qr", api_qr, ["GET"]),
        ("/status", api_status, ["GET"]),
        ("/logout", api_logout, ["POST"]),
        ("/desktop/start", api_desktop_start, ["POST"]),
        ("/desktop/frame", api_desktop_frame, ["GET"]),
        ("/desktop/input", api_desktop_input, ["POST"]),
        ("/desktop/resize", api_desktop_resize, ["POST"]),
        ("/desktop/reload", api_desktop_reload, ["POST"]),
        ("/desktop/save", api_desktop_save, ["POST"]),
        ("/desktop/lock", api_desktop_lock, ["POST"]),
    ):
        target_app.add_url_rule(
            f"{prefix}{suffix}",
            endpoint=f"web_login_{suffix[1:]}",
            view_func=view,
            methods=methods,
        )


# ── Embedded panel (used by file_browser's Login tab) ─────────────────

WEB_LOGIN_PANEL_HTML = r"""
<div id="webLoginPanelBody" class="web-login-panel">
  <div id="webLoginUnlock">
    <p class="web-login-hint">请输入 Web Login 密码以开始抖音扫码登录。</p>
    <form id="webLoginUnlockForm" class="web-login-form">
      <input id="webLoginPassword" type="password" autocomplete="current-password" placeholder="Web Login 密码" required>
      <button class="btn" id="webLoginUnlockButton" type="submit">验证并开始</button>
    </form>
    <div id="webLoginUnlockStatus" class="web-login-status">请输入密码后开始</div>
  </div>
  <div id="webLoginControls" style="display:none">
    <div id="webLoginDesktopBox" class="web-login-qr-box" tabindex="0">
      <span id="webLoginDesktopPlaceholder">正在启动远程桌面...</span>
      <img id="webLoginDesktopFrame" alt="抖音 Firefox 远程桌面" draggable="false" style="display:none">
    </div>
    <div id="webLoginStatus" class="web-login-status">尚未验证</div>
    <button class="btn" id="webLoginSave" type="button">💾 保存登录状态</button>
    <button class="btn" id="webLoginReload" type="button">🔄 刷新页面</button>
    <button class="setting-action" id="webLoginLogout" type="button">🔒 结束远程桌面/锁定</button>
    <p class="web-login-hint">这是服务器上的 Firefox 远程桌面镜像。请直接点击抖音页面中的登录并完成扫码；不会依赖固定中文按钮。</p>
  </div>
</div>
<script>
(function() {
  var timer = null, inFlight = false, started = false;
  var api = '/api/web-login';
  var status = document.getElementById('webLoginStatus');
  var unlockStatus = document.getElementById('webLoginUnlockStatus');
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  function setStatus(text, cls) { status.textContent = text; status.className = 'web-login-status ' + (cls || ''); }
  async function responseData(response) {
    var contentType = response.headers.get('content-type') || '';
    if (contentType.indexOf('application/json') >= 0) return response.json();
    // Flask's generic 403/404 pages are HTML.  Keep that implementation
    // detail out of the UI and point the operator at the useful remediation.
    if (response.status === 403) return {message: '请求被来源保护拒绝：请确认 FILE_BROWSER_ALLOWED_ORIGINS 包含当前访问地址。'};
    return {message: '服务器返回了非 JSON 响应（HTTP ' + response.status + '）'};
  }
  async function frame() {
    var image = document.getElementById('webLoginDesktopFrame');
    var placeholder = document.getElementById('webLoginDesktopPlaceholder');
    try {
      var response = await fetch(api + '/desktop/frame', {cache: 'no-store'}); var data = await responseData(response);
      if (!response.ok || !data.success) throw new Error(data.message || '截图失败');
      image.src = data.frame; image.style.display = 'block'; placeholder.style.display = 'none';
      setStatus('🖱️ 可直接操作服务器 Firefox 页面', 'wait');
    } catch (error) { placeholder.style.display = 'flex'; placeholder.textContent = '❌ ' + error.message; setStatus(error.message, 'err'); }
  }
  async function start() {
    stop();
    var placeholder = document.getElementById('webLoginDesktopPlaceholder');
    placeholder.style.display = 'flex'; placeholder.textContent = '⏳ 正在启动远程桌面...';
    try {
      var response = await fetch(api + '/desktop/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); var data = await responseData(response);
      if (!response.ok || !data.success) throw new Error(data.message || '启动远程桌面失败');
      started = true; document.getElementById('webLoginDesktopFrame').src = data.frame; document.getElementById('webLoginDesktopFrame').style.display = 'block'; placeholder.style.display = 'none';
      setStatus('🖱️ 可直接操作服务器 Firefox 页面', 'wait'); timer = setInterval(frame, 1500);
    } catch (error) { setStatus(error.message, 'err'); placeholder.textContent = '❌ 启动失败，请重试'; }
  }
  // Compatibility name retained for older embedded-page automation.
  function loadQr() { return start(); }
  async function sendInput(event) {
    if (!started) return;
    try {
      var response = await fetch(api + '/desktop/input', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(event)}); var data = await responseData(response);
      if (!response.ok || !data.success) setStatus(data.message || '输入发送失败', 'err'); else frame();
    } catch (error) { setStatus('输入发送失败', 'err'); }
  }
  function point(event) {
    var image = document.getElementById('webLoginDesktopFrame'), rect = image.getBoundingClientRect();
    return {x: Math.max(0, Math.min(1280, (event.clientX - rect.left) * 1280 / rect.width)), y: Math.max(0, Math.min(720, (event.clientY - rect.top) * 720 / rect.height))};
  }
  document.getElementById('webLoginUnlockForm').onsubmit = async function(event) {
    event.preventDefault(); var button = document.getElementById('webLoginUnlockButton'); button.disabled = true;
    try {
      var response = await fetch(api + '/unlock', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: document.getElementById('webLoginPassword').value})});
      var data = await responseData(response); if (!response.ok) throw new Error(data.message || '验证失败');
      document.getElementById('webLoginUnlock').style.display = 'none'; document.getElementById('webLoginControls').style.display = 'block'; loadQr();
    } catch (error) { unlockStatus.textContent = error.message; unlockStatus.className = 'web-login-status err'; button.disabled = false; }
  };
  document.getElementById('webLoginDesktopFrame').onclick = function(event) { var p = point(event); sendInput({kind: 'click', x: p.x, y: p.y, button: 'left', click_count: 1}); };
  document.getElementById('webLoginDesktopFrame').onwheel = function(event) { event.preventDefault(); sendInput({kind: 'wheel', delta_x: Math.max(-2000, Math.min(2000, event.deltaX)), delta_y: Math.max(-2000, Math.min(2000, event.deltaY))}); };
  document.getElementById('webLoginDesktopBox').onkeydown = function(event) { if (event.key && event.key.length <= 64) { event.preventDefault(); sendInput({kind: 'key', key: event.key}); } };
  document.getElementById('webLoginSave').onclick = async function() { var response = await fetch(api + '/desktop/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); var data = await responseData(response); setStatus(data.message || (data.success ? '登录状态已保存' : '保存失败'), data.success ? 'ok' : 'err'); };
  document.getElementById('webLoginReload').onclick = async function() { var response = await fetch(api + '/desktop/reload', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); var data = await responseData(response); if (response.ok && data.frame) { document.getElementById('webLoginDesktopFrame').src = data.frame; setStatus('页面已刷新', 'wait'); } else setStatus(data.message || '刷新失败', 'err'); };
  document.getElementById('webLoginLogout').onclick = async function() { stop(); started = false; await fetch(api + '/desktop/lock', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); document.getElementById('webLoginControls').style.display = 'none'; document.getElementById('webLoginUnlock').style.display = 'block'; document.getElementById('webLoginUnlockButton').disabled = false; };
})();
</script>
"""


# ── Single-page frontend (inline template — no separate file needed) ──

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖音扫码登录 — Douyin Email Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #0f0f0f; color: #e0e0e0;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh;
  }
  .card {
    background: #1a1a1a; border-radius: 16px; padding: 40px 32px;
    max-width: 1100px; width: 95%; text-align: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  h1 { font-size: 22px; font-weight: 600; margin-bottom: 8px; color: #fff; }
  .subtitle { font-size: 13px; color: #888; margin-bottom: 28px; }
  #qr-box {
    width: min(960px, 90vw); aspect-ratio: 16 / 9; margin: 0 auto 20px;
    border-radius: 12px; overflow: hidden; position: relative;
    background: #2a2a2a; display: flex; align-items: center; justify-content: center;
  }
  #qr-box a { width: 100%; height: 100%; display: block; }
  #qr-box img { width: 100%; height: 100%; object-fit: contain; cursor: zoom-in; }
  #qr-placeholder { color: #666; font-size: 14px; }
  .status { font-size: 14px; margin: 12px 0; min-height: 20px; }
  .status.ok { color: #4caf50; }
  .status.wait { color: #ff9800; }
  .status.err { color: #f44336; }
  .hint { font-size: 12px; color: #666; margin-top: 16px; line-height: 1.6; }
  .btn {
    display: inline-block; margin-top: 16px; padding: 10px 28px;
    border: none; border-radius: 8px; font-size: 14px; cursor: pointer;
    background: #fe2c55; color: #fff; text-decoration: none;
    transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .unlock-form { display:flex; justify-content:center; gap:8px; margin:22px 0; }
  .unlock-form input { min-width:240px; border:1px solid #555; border-radius:8px; padding:10px; background:#252525; color:#fff; }
  #login-panel { margin-top: 18px; }
</style>
</head>
<body>
<div class="card">
  <h1>抖音扫码登录</h1>
  <p class="subtitle">Douyin Email Bot — Cookie 获取</p>

  <div id="unlock-panel">
    <p class="hint">请输入部署配置的 Web Login 密码以开始抖音扫码登录</p>
    <form class="unlock-form" onsubmit="unlock(event)">
      <input id="password" type="password" autocomplete="current-password" placeholder="Web Login 密码" required>
      <button class="btn" id="unlock-btn" type="submit">验证并开始</button>
    </form>
    <div id="unlock-status" class="status wait">请输入密码后开始</div>
  </div>

  <div id="login-panel" style="display:none">
  <div id="qr-box">
    <a id="qr-link" href="#" target="_blank" title="点击打开原尺寸截图">
      <img id="qr-img" src="" alt="抖音完整登录页面" style="display:none">
    </a>
    <div id="qr-placeholder">⏳ 加载中...</div>
  </div>

  <div id="status" class="status wait">正在生成二维码...</div>

  <button id="refresh-btn" class="btn" onclick="loadQR()" style="display:none">
    🔄 刷新二维码
  </button>

  <p class="hint">
    使用 <b>抖音 App</b> 扫描二维码<br>
    扫码成功后 Cookie 将自动保存到运行时设置
  </p>
  <button id="logout-btn" class="btn" onclick="logout()">🔒 锁定</button>
  </div>
</div>

<script>
let _pollTimer = null;
let _pollInFlight = false;

async function unlock(event) {
  event.preventDefault();
  const status = document.getElementById('unlock-status');
  const button = document.getElementById('unlock-btn');
  button.disabled = true;
  status.textContent = '正在验证...';
  try {
    const resp = await fetch('/api/unlock', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: document.getElementById('password').value})
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.message || '验证失败');
    document.getElementById('unlock-panel').style.display = 'none';
    document.getElementById('login-panel').style.display = 'block';
    loadQR();
  } catch (e) {
    status.textContent = e.message;
    status.className = 'status err';
    button.disabled = false;
  }
}

async function logout() {
  stopPolling();
  await fetch('/api/logout', {method: 'POST'});
  document.getElementById('login-panel').style.display = 'none';
  document.getElementById('unlock-panel').style.display = 'block';
  document.getElementById('status').textContent = '已锁定，请重新验证';
  document.getElementById('status').className = 'status wait';
  document.getElementById('unlock-btn').disabled = false;
}

async function loadQR() {
  const img = document.getElementById('qr-img');
  const link = document.getElementById('qr-link');
  const placeholder = document.getElementById('qr-placeholder');
  const status = document.getElementById('status');
  const refreshBtn = document.getElementById('refresh-btn');

  img.style.display = 'none';
  placeholder.style.display = 'flex';
  placeholder.textContent = '⏳ 生成二维码...';
  status.textContent = '正在生成二维码...';
  status.className = 'status wait';
  refreshBtn.style.display = 'none';
  stopPolling();

  try {
    const resp = await fetch('/api/qr');
    const data = await resp.json();
    if (data.success) {
      img.src = data.qr_image;
      link.href = data.qr_image;
      img.style.display = 'block';
      placeholder.style.display = 'none';
      status.textContent = '完整登录页面（点击图片可打开原尺寸）';
      status.className = 'status wait';
      refreshBtn.style.display = 'inline-block';
      startPolling();
    } else {
      placeholder.textContent = '❌ 生成失败';
      status.textContent = data.message || '生成二维码失败，请重试';
      status.className = 'status err';
      refreshBtn.style.display = 'inline-block';
    }
  } catch (e) {
    placeholder.textContent = '❌ 网络错误';
    status.textContent = '无法连接服务器: ' + e.message;
    status.className = 'status err';
    refreshBtn.style.display = 'inline-block';
  }
}

function startPolling() {
  stopPolling();
  _pollTimer = setInterval(pollStatus, 10000);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function pollStatus() {
  if (_pollInFlight) return;
  _pollInFlight = true;
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    const el = document.getElementById('status');

    if (data.status === 'logged_in') {
      stopPolling();
      el.textContent = '✅ 登录成功！Cookie 已保存 (' + data.auth_count + ' 个认证 token)';
      el.className = 'status ok';

      document.getElementById('refresh-btn').style.display = 'none';
      document.getElementById('qr-img').style.opacity = '0.4';
    } else if (data.status === 'expired') {
      el.textContent = '⚠️ 二维码已过期，正在自动刷新...';
      el.className = 'status wait';
      setTimeout(loadQR, 1000);
    } else if (data.status === 'pending') {
      el.textContent = '⏳ ' + (data.message || '等待扫码...');
      el.className = 'status wait';
    } else {
      el.textContent = '❌ ' + (data.message || '检查失败');
      el.className = 'status err';
    }
  } catch (e) {
    // network error during poll, ignore and keep trying
  } finally {
    _pollInFlight = false;
  }
}

// QR capture intentionally starts only after password verification.
</script>
</body>
</html>"""


# ── Entrypoint ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Douyin QR Web Login Service")
    parser.add_argument("--port", type=int, default=8080, help="Listen port (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    profile_dir = _get_profile_dir()
    log.info("Starting QR login service on http://%s:%d", args.host, args.port)
    log.info("Firefox profile: %s", profile_dir)

    if not profile_dir.exists():
        log.info("Profile directory will be created on first QR capture")
    elif (profile_dir / "cookies.sqlite").exists():
        log.info("Existing login state found — may already be logged in")

    log.info("Open http://<host>:%d in a browser to scan the QR code", args.port)

    try:
        app.run(host=args.host, port=args.port, debug=args.debug, threaded=False)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
