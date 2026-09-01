"""Web-based Douyin QR code login service.

Replaces get_cookie.py's interactive login (which blocks on input()) with
a web interface: open http://<host>:8080, scan the QR code with the Douyin
app, and cookies are automatically saved to the managed runtime settings store.

Usage:
    python web_login.py [--port 8080] [--host 127.0.0.1]
"""

import argparse
from collections import deque
import hashlib
import hmac
import logging
import math
import os
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
_RATE_LIMIT_DEFAULTS = {"unlock": 5, "qr": 5, "status": 120}
_RATE_LIMIT_ENV_VARS = {
    "unlock": "WEB_LOGIN_PASSWORD_RATE_LIMIT",
    "qr": "WEB_LOGIN_QR_RATE_LIMIT",
    "status": "WEB_LOGIN_STATUS_RATE_LIMIT",
}
_DEFAULT_RATE_WINDOW_SECONDS = 60
_SESSION_SECONDS = 15 * 60
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], deque[float]] = {}


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
    for kind in ("unlock", "qr", "status", "logout"):
        if path == f"/api/{kind}" or path.endswith(f"/api/web-login/{kind}"):
            return kind
    return None


def _unauthorized(message="请先验证登录密码"):
    response = jsonify({"success": False, "message": message})
    response.headers["Cache-Control"] = "no-store"
    return response, 401


def _service_unavailable():
    response = jsonify({"success": False, "message": "Web Login 未配置密码"})
    response.headers["Cache-Control"] = "no-store"
    return response, 503


def _protect_login_request():
    kind = _login_api_kind(request.path)
    if kind is None:
        return None
    if not _has_allowed_source():
        response = jsonify({"success": False, "message": "请求来源不被允许"})
        response.headers["Cache-Control"] = "no-store"
        return response, 403
    if kind in {"unlock", "qr", "status"} and not _password():
        return _service_unavailable()
    if kind in _RATE_LIMIT_ENV_VARS:
        limited = _rate_limit_response(kind)
        if limited:
            return limited
    if kind in {"qr", "status"} and not session.get("web_login_unlocked"):
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
    response = jsonify({"success": True, "message": "验证成功"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Relock the QR controls and invalidate the current session."""
    session.clear()
    response = jsonify({"success": True})
    response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/")
def index():
    """Serve the QR login page."""
    return render_template_string(LOGIN_HTML)


@app.route("/api/qr")
def api_qr():
    """Generate a fresh QR code screenshot."""
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
    <div id="webLoginQrBox" class="web-login-qr-box"><span id="webLoginQrPlaceholder">输入密码后生成二维码</span><img id="webLoginQrImage" alt="抖音完整登录页面" style="display:none"></div>
    <div id="webLoginStatus" class="web-login-status">尚未验证</div>
    <button class="btn" id="webLoginRefresh" type="button">🔄 刷新二维码</button>
    <button class="setting-action" id="webLoginLogout" type="button">🔒 锁定</button>
    <p class="web-login-hint">使用抖音 App 扫描二维码，Cookie 将自动保存到运行时设置。</p>
  </div>
</div>
<script>
(function() {
  var timer = null, inFlight = false;
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
  async function loadQr() {
    stop();
    var image = document.getElementById('webLoginQrImage');
    var placeholder = document.getElementById('webLoginQrPlaceholder');
    placeholder.textContent = '⏳ 生成二维码...'; image.style.display = 'none';
    try {
      var response = await fetch(api + '/qr', {cache: 'no-store'}); var data = await responseData(response);
      if (!response.ok || !data.success) throw new Error(data.message || '生成二维码失败');
      image.src = data.qr_image; image.style.display = 'block'; placeholder.textContent = '';
      setStatus('请使用抖音 App 扫描二维码', 'wait'); timer = setInterval(poll, 10000);
    } catch (error) { placeholder.textContent = '❌ 生成失败'; setStatus(error.message, 'err'); }
  }
  async function poll() {
    if (inFlight) return; inFlight = true;
    try {
      var response = await fetch(api + '/status', {cache: 'no-store'}); var data = await responseData(response);
      if (!response.ok) { stop(); setStatus(data.message || '会话已锁定', 'err'); return; }
      if (data.status === 'logged_in') { stop(); setStatus('✅ 登录成功！Cookie 已保存 (' + data.auth_count + ' 个认证 token)', 'ok'); }
      else if (data.status === 'expired') { setStatus('二维码已过期，正在刷新...', 'wait'); setTimeout(loadQr, 1000); }
      else if (data.status === 'pending') setStatus('⏳ ' + (data.message || '等待扫码...'), 'wait');
      else setStatus(data.message || '检查失败', 'err');
    } catch (error) { /* keep polling after transient network failures */ }
    finally { inFlight = false; }
  }
  document.getElementById('webLoginUnlockForm').onsubmit = async function(event) {
    event.preventDefault(); var button = document.getElementById('webLoginUnlockButton'); button.disabled = true;
    try {
      var response = await fetch(api + '/unlock', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: document.getElementById('webLoginPassword').value})});
      var data = await responseData(response); if (!response.ok) throw new Error(data.message || '验证失败');
      document.getElementById('webLoginUnlock').style.display = 'none'; document.getElementById('webLoginControls').style.display = 'block'; loadQr();
    } catch (error) { unlockStatus.textContent = error.message; unlockStatus.className = 'web-login-status err'; button.disabled = false; }
  };
  document.getElementById('webLoginRefresh').onclick = loadQr;
  document.getElementById('webLoginLogout').onclick = async function() { stop(); await fetch(api + '/logout', {method: 'POST'}); document.getElementById('webLoginControls').style.display = 'none'; document.getElementById('webLoginUnlock').style.display = 'block'; document.getElementById('webLoginUnlockButton').disabled = false; };
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
