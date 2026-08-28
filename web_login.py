"""Web-based Douyin QR code login service.

Replaces get_cookie.py's interactive login (which blocks on input()) with
a web interface: open http://<host>:8080, scan the QR code with the Douyin
app, and cookies are automatically saved to the managed runtime settings store.

Usage:
    python web_login.py [--port 8080] [--host 127.0.0.1]
"""

import argparse
from collections import deque
import logging
import math
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template_string, request

# ── Bootstrap: same as main.py (needed before any F2 imports) ─────
_PROJECT_DIR = Path(__file__).parent

# Load config for profile_dir
from config_loader import load_config  # noqa: E402
from settings_store import SettingsStore, default_database_path  # noqa: E402

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
_RATE_LIMIT_DEFAULTS = {"/api/qr": 5, "/api/status": 120}
_RATE_LIMIT_ENV_VARS = {
    "/api/qr": "WEB_LOGIN_QR_RATE_LIMIT",
    "/api/status": "WEB_LOGIN_STATUS_RATE_LIMIT",
}
_DEFAULT_RATE_WINDOW_SECONDS = 60
_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], deque[float]] = {}


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


def _rate_limit_response(path: str):
    """Return a 429 response when this client has exhausted the endpoint quota."""
    limit = _positive_env_int(_RATE_LIMIT_ENV_VARS[path], _RATE_LIMIT_DEFAULTS[path])
    window = _positive_env_int("WEB_LOGIN_RATE_WINDOW_SECONDS", _DEFAULT_RATE_WINDOW_SECONDS)
    client = request.remote_addr or "unknown"
    now = time.monotonic()
    bucket_key = (path, client)

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


@app.before_request
def _protect_api_routes():
    if request.path not in _RATE_LIMIT_ENV_VARS:
        return None
    if not _has_allowed_source():
        response = jsonify({"success": False, "message": "请求来源不被允许"})
        response.headers["Cache-Control"] = "no-store"
        return response, 403
    return _rate_limit_response(request.path)


# ── Routes ─────────────────────────────────────────────────────────

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
</style>
</head>
<body>
<div class="card">
  <h1>抖音扫码登录</h1>
  <p class="subtitle">Douyin Email Bot — Cookie 获取</p>

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
</div>

<script>
let _pollTimer = null;
let _pollInFlight = false;

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

// Kick off on page load
loadQR();
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
