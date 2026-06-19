"""EmailBot — polls an IMAP inbox for Douyin links and replies via SMTP.

Also supports cookie management commands via email:
- "更新cookie" in subject → body contains new cookie → write to .env
- "自动获取cookie" in subject → extract from browser on this machine
"""

import email
import imaplib
import logging
import os
import re
import smtplib
import time
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

from douyin_downloader import DouyinDownloader
from url_extractor import UrlExtractor

logger = logging.getLogger("EmailBot")

_ADDR_RE = re.compile(r"<([^>]+)>")

# Cookie domain filter for browser extraction
_DOUYIN_COOKIE_DOMAINS = {".douyin.com", "douyin.com", "www.douyin.com"}


class EmailBot:
    """Monitors an inbox for emails containing Douyin links and downloads videos.

    Also handles cookie management commands.
    """

    def __init__(self, config):
        self.config = config
        self.downloader = DouyinDownloader(config.douyin)
        self.extractor = UrlExtractor()
        self._cooldowns: dict[str, float] = {}
        self._project_dir = Path(__file__).parent

    def run(self) -> None:
        cfg = self.config.email
        bot_cfg = self.config.bot

        logger.info(
            "EmailBot starting — mailbox: %s, poll interval: %ds",
            cfg.email,
            cfg.poll_interval,
        )

        while True:
            try:
                self._poll_once(cfg, bot_cfg)
            except imaplib.IMAP4.error as e:
                logger.error("IMAP error: %s — retrying in %ds", e, cfg.poll_interval)
            except smtplib.SMTPException as e:
                logger.error("SMTP error: %s", e)
            except (ConnectionError, OSError) as e:
                logger.error("Network error: %s — retrying in %ds", e, cfg.poll_interval)
            except Exception:
                logger.exception("Unexpected error during poll cycle")

            time.sleep(cfg.poll_interval)

    # ── Poll cycle ────────────────────────────────────────────────

    def _poll_once(self, cfg, bot_cfg) -> None:
        mail = self._imap_connect(cfg)
        try:
            mail.select("INBOX")
            status, data = mail.search(None, "UNSEEN")
            if status != "OK":
                return

            msg_ids = data[0].split()
            if not msg_ids:
                return

            logger.info("Found %d unseen email(s)", len(msg_ids))

            for msg_id in msg_ids:
                self._process_email(mail, msg_id, cfg, bot_cfg)
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _process_email(self, mail, msg_id: bytes, cfg, bot_cfg) -> None:
        status, data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            return

        raw = data[0][1]
        msg = email.message_from_bytes(raw)

        sender = _extract_addr(msg.get("From", ""))
        subject = _decode_str(msg.get("Subject", ""))

        if not sender:
            return

        # Sender allowlist
        allowed = bot_cfg.allowed_senders
        if allowed and sender not in allowed:
            logger.debug("Skipping email from non-allowed sender: %s", sender)
            return

        body = _get_body_text(msg)
        commands = bot_cfg.commands

        # ── Command: cookie update (manual paste) ──────────────────
        if commands.cookie_update and commands.cookie_update in subject:
            self._handle_cookie_update(mail, msg_id, cfg, sender, body)
            return

        # ── Command: cookie auto-extract from browser ──────────────
        if commands.cookie_auto and commands.cookie_auto in subject:
            self._handle_cookie_auto(mail, msg_id, cfg, sender, body)
            return

        # ── Normal: download ───────────────────────────────────────
        keyword = bot_cfg.subject_keyword
        if keyword and keyword not in subject:
            logger.debug("Skipping — subject missing keyword '%s': %s", keyword, subject)
            return

        url = self.extractor.extract(subject + " " + body)
        if url is None:
            logger.info("No Douyin URL found from %s (subject: %s)", sender, subject)
            self._send_reply(cfg, sender, "未在邮件中找到抖音分享链接，请检查链接是否正确。")
            _mark_seen(mail, msg_id)
            return

        logger.info("Douyin URL from %s: %s", sender, url)

        # Cooldown
        now = time.time()
        if sender in self._cooldowns:
            elapsed = now - self._cooldowns[sender]
            if elapsed < bot_cfg.cooldown_seconds:
                remaining = int(bot_cfg.cooldown_seconds - elapsed)
                logger.info("Sender %s in cooldown (%ds remaining)", sender, remaining)
                return

        result = self.downloader.download(url)

        if result["success"]:
            self._cooldowns[sender] = time.time()
            filepath = result["filepath"] or "未知路径"
            self._send_reply(
                cfg, sender,
                f"下载完成！\n标题：{result['title']}\n保存位置：{filepath}",
            )
        else:
            error_msg = result["error"]
            # If the error mentions cookie, hint the user
            if "cookie" in error_msg.lower():
                error_msg += "\n\n提示：发送主题含「更新cookie」的邮件并粘贴新 cookie 即可更新。"
            self._send_reply(cfg, sender, f"下载失败：{error_msg}")

        _mark_seen(mail, msg_id)

    # ── Cookie command handlers ───────────────────────────────────

    def _handle_cookie_update(self, mail, msg_id, cfg, sender, body) -> None:
        """Extract new cookie from email body, write to .env, hot-reload."""
        new_cookie = body.strip()
        if not new_cookie:
            self._send_reply(cfg, sender, "邮件正文为空，请粘贴新的 cookie 后重试。")
            _mark_seen(mail, msg_id)
            return

        if len(new_cookie) < 100:
            logger.warning("Cookie from %s looks too short (%d chars)", sender, len(new_cookie))
            self._send_reply(
                cfg, sender,
                f"收到的 cookie 似乎不完整（仅 {len(new_cookie)} 字符），请确认已粘贴完整的 cookie 字符串。\n\n"
                "获取方式: 浏览器登录 douyin.com → F12 → 控制台 → 输入 document.cookie → 复制全部输出。",
            )
            _mark_seen(mail, msg_id)
            return

        ok = _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", new_cookie)
        if not ok:
            self._send_reply(cfg, sender, "写入 .env 文件失败，请检查文件权限。")
            _mark_seen(mail, msg_id)
            return

        # Hot-reload into the running downloader
        self.downloader.config.cookie = new_cookie
        os.environ["DOUYIN_COOKIE"] = new_cookie

        logger.info("Cookie updated by %s (%d chars)", sender, len(new_cookie))
        self._send_reply(
            cfg, sender,
            f"Cookie 已更新！（{len(new_cookie)} 字符）\n有效期通常 24-48 小时，过期后请重新发送。",
        )
        _mark_seen(mail, msg_id)

    def _handle_cookie_auto(self, mail, msg_id, cfg, sender, body) -> None:
        """Try to auto-extract cookie from browsers on this machine."""
        self._send_reply(cfg, sender, "正在尝试从浏览器自动获取 cookie，请稍候...")

        # Try browsers in order of success probability on Windows
        cookie_str = _try_extract_cookie()

        if cookie_str:
            ok = _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", cookie_str)
            if ok:
                self.downloader.config.cookie = cookie_str
                os.environ["DOUYIN_COOKIE"] = cookie_str
                logger.info("Cookie auto-extracted (%d chars)", len(cookie_str))
                self._send_reply(
                    cfg, sender,
                    f"Cookie 已自动获取并更新！（{len(cookie_str)} 字符）\n来源：浏览器自动提取",
                )
            else:
                self._send_reply(cfg, sender, "Cookie 已提取但写入 .env 失败，请检查文件权限。")
        else:
            self._send_reply(
                cfg, sender,
                "自动获取失败：未找到已登录抖音的浏览器。\n\n"
                "请确保已用浏览器登录 douyin.com，然后重试。\n"
                "或使用方法二：发送主题含「更新cookie」的邮件，正文粘贴 document.cookie 的输出。",
            )

        _mark_seen(mail, msg_id)

    # ── IMAP / SMTP helpers ───────────────────────────────────────

    def _imap_connect(self, cfg):
        logger.debug("Connecting to IMAP %s:%d", cfg.imap_server, cfg.imap_port)
        mail = imaplib.IMAP4_SSL(cfg.imap_server, cfg.imap_port)
        mail.login(cfg.email, cfg.password)
        return mail

    def _send_reply(self, cfg, to_addr: str, body: str) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = cfg.email
        msg["To"] = to_addr
        msg["Subject"] = "Re: 抖音视频下载"

        logger.debug("Sending reply to %s", to_addr)
        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(cfg.email, cfg.password)
            smtp.send_message(msg)

        logger.info("Reply sent to %s", to_addr)


# ── Email parsing utilities ───────────────────────────────────────

def _extract_addr(from_header: str) -> str:
    m = _ADDR_RE.search(from_header)
    return m.group(1) if m else from_header.strip()


def _decode_str(header: str) -> str:
    parts = decode_header(header)
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            result.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(text)
    return "".join(result)


def _get_body_text(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def _mark_seen(mail, msg_id: bytes) -> None:
    try:
        mail.store(msg_id, "+FLAGS", "\\Seen")
    except Exception:
        logger.warning("Failed to mark message %s as seen", msg_id)


# ── .env file utilities ───────────────────────────────────────────

def _write_env(env_path: Path, key: str, value: str) -> bool:
    """Update or add a key=value line in a dotenv file.

    Returns True on success, False on I/O error.
    """
    try:
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return True
    except OSError as e:
        logger.error("Failed to write %s: %s", env_path, e)
        return False


# ── Browser cookie extraction ─────────────────────────────────────

def _try_extract_cookie() -> str | None:
    """Try to extract douyin.com cookies from installed browsers.

    Tries Firefox first (simpler cookie storage, no encryption),
    then Chrome, then Edge. Returns a semicolon-joined cookie string
    or None if all browsers fail.
    """
    try:
        import browser_cookie3  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("browser_cookie3 not available")
        return None

    # Order: Firefox first on Windows (least likely to encrypt),
    # then Chrome, then Edge
    browsers = [
        ("Firefox", "firefox"),
        ("Chrome", "chrome"),
        ("Edge", "edge"),
    ]

    for name, browser_key in browsers:
        try:
            cj = getattr(browser_cookie3, browser_key)()
            cookies = []
            for c in cj:
                if c.domain in _DOUYIN_COOKIE_DOMAINS:
                    cookies.append(f"{c.name}={c.value}")
            if cookies:
                logger.info("Extracted %d douyin cookies from %s", len(cookies), name)
                return "; ".join(cookies)
        except Exception as e:
            logger.debug("Failed to extract cookies from %s: %s", name, e)

    return None
