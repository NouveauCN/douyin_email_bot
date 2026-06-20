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

from colorama import Fore, Style
from douyin_downloader import DouyinDownloader
from url_extractor import UrlExtractor

logger = logging.getLogger("EmailBot")

_ADDR_RE = re.compile(r"<([^>]+)>")



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
        self._seen_ids: set[str] = set()  # dedup across poll cycles

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

            logger.debug("Found %d unseen email(s)", len(msg_ids))

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

        # ── Skip own replies (avoid infinite loop) ──────────────────
        if sender == cfg.email:
            logger.debug("Skipping own email: %s", subject)
            _mark_seen(mail, msg_id)
            return

        # ── Dedup: skip already-processed message IDs ───────────────
        msg_id_str = msg_id.decode("ascii", errors="replace") if isinstance(msg_id, bytes) else str(msg_id)
        if msg_id_str in self._seen_ids:
            logger.debug("Skipping already-processed message: %s", msg_id_str)
            _mark_seen(mail, msg_id)
            return
        self._seen_ids.add(msg_id_str)

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

        logger.info(f"{Fore.CYAN}收到下载请求: %s", sender)

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
            logger.info(
                f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载成功: %s -> %s",
                result["title"],
                filepath,
            )
            self._send_reply(
                cfg, sender,
                f"下载完成！\n标题：{result['title']}\n保存位置：{filepath}",
            )
        else:
            error_msg = result["error"]
            # ── Auto-refresh cookie and retry once ──────────────────
            is_cookie_issue = (
                "删" in error_msg
                or "私密" in error_msg
                or "cookie" in error_msg.lower()
                or "异常" in error_msg
            )
            if is_cookie_issue:
                logger.info("Attempting auto cookie refresh from Firefox profile...")
                refreshed_cookie, refresh_msg = _try_extract_cookie(
                    profile_dir=self.config.cookie_extractor.profile_dir or None,
                )
                if refreshed_cookie and refreshed_cookie != self.downloader.config.cookie:
                    # Hot-reload new cookie and retry
                    self.downloader.config.cookie = refreshed_cookie
                    os.environ["DOUYIN_COOKIE"] = refreshed_cookie
                    _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", refreshed_cookie)
                    logger.info(
                        "Cookie refreshed (%d chars → %d chars), retrying download...",
                        len(self.config.douyin.cookie), len(refreshed_cookie),
                    )
                    retry_result = self.downloader.download(url)
                    if retry_result["success"]:
                        self._cooldowns[sender] = time.time()
                        filepath = retry_result["filepath"] or "未知路径"
                        logger.info(
                            f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载成功 (cookie 刷新后): %s -> %s",
                            retry_result["title"],
                            filepath,
                        )
                        self._send_reply(
                            cfg, sender,
                            f"下载完成！（cookie 已自动刷新）\n标题：{retry_result['title']}\n保存位置：{filepath}",
                        )
                        _mark_seen(mail, msg_id)
                        return
                    else:
                        logger.warning("Retry after cookie refresh also failed: %s", retry_result["error"])
                        error_msg += f"\n（已尝试自动刷新 cookie 并重试，仍失败）"

            # Build helpful hints
            error_msg += (
                "\n\n解决方案："
                "\n1. 发送主题含「更新cookie」的邮件，正文粘贴浏览器中获取的完整 cookie"
                "\n2. 发送主题含「自动获取cookie」的邮件，让机器人从 Firefox 配置文件提取"
            )
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
        """Try to auto-extract cookie via headless Playwright Firefox."""
        self._send_reply(cfg, sender, "正在尝试自动获取 cookie（无头 Firefox）...")

        cookie_str, status_msg = _try_extract_cookie(
            profile_dir=self.config.cookie_extractor.profile_dir or None,
        )

        if cookie_str:
            ok = _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", cookie_str)
            if ok:
                self.downloader.config.cookie = cookie_str
                os.environ["DOUYIN_COOKIE"] = cookie_str
                logger.info("Cookie auto-extracted (%d chars): %s", len(cookie_str), status_msg)
                self._send_reply(
                    cfg, sender,
                    f"Cookie 已自动获取并更新！（{len(cookie_str)} 字符）\n来源：{status_msg}",
                )
            else:
                self._send_reply(cfg, sender, "Cookie 已提取但写入 .env 失败，请检查文件权限。")
        else:
            self._send_reply(
                cfg, sender,
                f"自动获取失败：{status_msg}\n\n"
                "方案一：在宿主机终端运行 uv run python get_cookie.py\n"
                "方案二：发送主题含「更新cookie」的邮件，正文粘贴 document.cookie 的输出。",
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

        logger.debug("Reply sent to %s", to_addr)


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
    """Mark an email as read. Tries two methods for compatibility."""
    msg_str = msg_id.decode("ascii", errors="replace") if isinstance(msg_id, bytes) else str(msg_id)
    try:
        mail.store(msg_id, "+FLAGS", "\\Seen")
    except Exception:
        try:
            # Some IMAP servers need the flag without backslash
            mail.store(msg_id, "+FLAGS", "Seen")
        except Exception:
            logger.warning("Failed to mark message as seen: %s", msg_str)


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

def _try_extract_cookie(profile_dir: Path | None = None) -> tuple[str | None, str]:
    """Extract douyin.com cookies via headless Playwright Firefox.

    Args:
        profile_dir: Firefox profile directory (None = use default).

    Returns:
        (cookie_string_or_None, status_message).
    """
    from cookie_extractor import extract_cookies  # noqa: E402

    return extract_cookies(profile_dir=profile_dir, headless=True, validate=True)
