"""Web-based Douyin QR code login service.

Replaces get_cookie.py's interactive login (which blocks on input()) with
a web interface: open http://<host>:8080, scan the QR code with the Douyin
app, and cookies are automatically saved to .env.

Usage:
    python web_login.py [--port 8080] [--host 0.0.0.0]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request

# ── Bootstrap: same as main.py (needed before any F2 imports) ─────
_PROJECT_DIR = Path(__file__).parent

# Load .env
_env_path = _PROJECT_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Load config for profile_dir
from config_loader import load_config  # noqa: E402
_config = load_config(_PROJECT_DIR / "config.yaml")

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


def _get_profile_dir() -> Path:
    raw = _config.cookie_extractor.profile_dir
    if raw:
        return Path(raw)
    return Path.home() / ".douyin_email_bot" / "firefox_profile"


# ── Routes ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the QR login page."""
    return render_template_string(LOGIN_HTML)


@app.route("/api/qr")
def api_qr():
    """Generate a fresh QR code screenshot."""
    profile_dir = _get_profile_dir()
    b64, msg = screenshot_qr_code(profile_dir)
    if b64:
        log.info("QR screenshot: %d chars", len(b64))
        return jsonify({"success": True, "qr_image": b64, "message": msg})
    log.error("QR screenshot failed: %s", msg)
    return jsonify({"success": False, "message": msg}), 500


@app.route("/api/status")
def api_status():
    """Check whether the user has scanned the QR and logged in."""
    profile_dir = _get_profile_dir()
    result = check_auth_cookies(profile_dir)

    if result["status"] == "logged_in" and result["cookie_str"]:
        # Validate cookie quality before saving
        grade, _ = _assess_quality(result["cookie_str"])
        result["message"] += f" — {grade}"

        # Persist to .env
        _write_env(str(_env_path), "DOUYIN_COOKIE", result["cookie_str"])
        os.environ["DOUYIN_COOKIE"] = result["cookie_str"]
        log.info(
            "Login success! Cookie saved (%d chars, %d auth tokens) — %s",
            len(result["cookie_str"]), result["auth_count"], grade,
        )

    return jsonify(result)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Graceful shutdown (for containerised use)."""
    log.info("Shutdown requested via /api/stop")
    return jsonify({"success": True, "message": "Shutting down..."})


# ── .env writeback ─────────────────────────────────────────────────

def _write_env(env_path: str, key: str, value: str) -> None:
    """Update or add key=value in .env file."""
    p = Path(env_path)
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        p.write_text(f"{key}={value}\n", encoding="utf-8")


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
    max-width: 400px; width: 90%; text-align: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  h1 { font-size: 22px; font-weight: 600; margin-bottom: 8px; color: #fff; }
  .subtitle { font-size: 13px; color: #888; margin-bottom: 28px; }
  #qr-box {
    width: 240px; height: 240px; margin: 0 auto 20px;
    border-radius: 12px; overflow: hidden; position: relative;
    background: #2a2a2a; display: flex; align-items: center; justify-content: center;
  }
  #qr-box img { width: 100%; height: 100%; object-fit: contain; }
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
  .cookie-preview {
    margin-top: 16px; padding: 12px; background: #111; border-radius: 8px;
    font-size: 11px; color: #4caf50; word-break: break-all; text-align: left;
    max-height: 100px; overflow-y: auto; display: none;
  }
</style>
</head>
<body>
<div class="card">
  <h1>抖音扫码登录</h1>
  <p class="subtitle">Douyin Email Bot — Cookie 获取</p>

  <div id="qr-box">
    <img id="qr-img" src="" alt="QR Code" style="display:none">
    <div id="qr-placeholder">⏳ 加载中...</div>
  </div>

  <div id="status" class="status wait">正在生成二维码...</div>
  <div id="cookie-preview" class="cookie-preview"></div>

  <button id="refresh-btn" class="btn" onclick="loadQR()" style="display:none">
    🔄 刷新二维码
  </button>

  <p class="hint">
    使用 <b>抖音 App</b> 扫描二维码<br>
    扫码成功后 Cookie 将自动保存到 .env
  </p>
</div>

<script>
let _pollTimer = null;

async function loadQR() {
  const img = document.getElementById('qr-img');
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
      img.style.display = 'block';
      placeholder.style.display = 'none';
      status.textContent = '请使用抖音 App 扫描二维码';
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
  _pollTimer = setInterval(pollStatus, 2500);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function pollStatus() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    const el = document.getElementById('status');
    const cp = document.getElementById('cookie-preview');

    if (data.status === 'logged_in') {
      stopPolling();
      el.textContent = '✅ 登录成功！Cookie 已保存 (' + data.auth_count + ' 个认证 token)';
      el.className = 'status ok';

      if (data.cookie_str) {
        cp.textContent = 'Cookie: ' + data.cookie_str.substring(0, 200) + '...';
        cp.style.display = 'block';
      }

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
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
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
        app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
