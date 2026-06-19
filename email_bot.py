"""EmailBot — polls an IMAP inbox for Douyin links and replies via SMTP."""

import email
import imaplib
import logging
import re
import smtplib
import time
from email.header import decode_header
from email.mime.text import MIMEText

from douyin_downloader import DouyinDownloader
from url_extractor import UrlExtractor

logger = logging.getLogger("EmailBot")

# Pattern to extract a bare email address from "Name <addr>" format
_ADDR_RE = re.compile(r"<([^>]+)>")


class EmailBot:
    """Monitors an inbox for emails containing Douyin links and downloads videos.

    Polls via IMAP, replies via SMTP. Designed for QQ Mail but works
    with any IMAP/SMTP provider.
    """

    def __init__(self, config):
        """
        Args:
            config: AppConfig dataclass instance.
        """
        self.config = config
        self.downloader = DouyinDownloader(config.douyin)
        self.extractor = UrlExtractor()
        self._cooldowns: dict[str, float] = {}

    def run(self) -> None:
        """Start the polling loop. Blocks until interrupted (Ctrl+C)."""
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
        """Connect, scan unseen emails, process matching ones, disconnect."""
        mail = self._imap_connect(cfg)
        try:
            mail.select("INBOX")

            # Search for unseen messages
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
        """Fetch, parse, filter, and handle a single email."""
        status, data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            return

        raw = data[0][1]
        msg = email.message_from_bytes(raw)

        # Extract sender address
        sender = _extract_addr(msg.get("From", ""))
        subject = _decode_str(msg.get("Subject", ""))

        if not sender:
            return

        # Filter: sender in allowlist
        allowed = bot_cfg.allowed_senders
        if allowed and sender not in allowed:
            logger.debug("Skipping email from non-allowed sender: %s", sender)
            return

        # Filter: subject must contain keyword
        keyword = bot_cfg.subject_keyword
        if keyword and keyword not in subject:
            logger.debug("Skipping email — subject missing keyword '%s': %s", keyword, subject)
            return

        # Extract body text
        body = _get_body_text(msg)

        # Extract Douyin URL
        url = self.extractor.extract(subject + " " + body)
        if url is None:
            logger.info("No Douyin URL found in email from %s (subject: %s)", sender, subject)
            self._send_reply(cfg, sender, "未在邮件中找到抖音分享链接，请检查链接是否正确。")
            _mark_seen(mail, msg_id)
            return

        logger.info("Douyin URL from %s: %s", sender, url)

        # Cooldown check
        now = time.time()
        if sender in self._cooldowns:
            elapsed = now - self._cooldowns[sender]
            if elapsed < bot_cfg.cooldown_seconds:
                remaining = int(bot_cfg.cooldown_seconds - elapsed)
                logger.info("Sender %s in cooldown (%ds remaining)", sender, remaining)
                return  # Silently skip — don't mark as seen so it can retry later

        # Download
        result = self.downloader.download(url)

        if result["success"]:
            self._cooldowns[sender] = time.time()
            filepath = result["filepath"] or "未知路径"
            self._send_reply(
                cfg,
                sender,
                f"下载完成！\n"
                f"标题：{result['title']}\n"
                f"保存位置：{filepath}",
            )
        else:
            self._send_reply(cfg, sender, f"下载失败：{result['error']}")

        # Mark as processed
        _mark_seen(mail, msg_id)

    # ── IMAP / SMTP helpers ───────────────────────────────────────

    def _imap_connect(self, cfg):
        """Open a new IMAP SSL connection and log in."""
        logger.debug("Connecting to IMAP %s:%d", cfg.imap_server, cfg.imap_port)
        mail = imaplib.IMAP4_SSL(cfg.imap_server, cfg.imap_port)
        mail.login(cfg.email, cfg.password)
        return mail

    def _send_reply(self, cfg, to_addr: str, body: str) -> None:
        """Send a plain-text reply via SMTP."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = cfg.email
        msg["To"] = to_addr
        msg["Subject"] = "Re: 抖音视频下载"

        logger.debug("Sending reply to %s via SMTP %s:%d", to_addr, cfg.smtp_server, cfg.smtp_port)
        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(cfg.email, cfg.password)
            smtp.send_message(msg)

        logger.info("Reply sent to %s", to_addr)


# ── Email parsing utilities ───────────────────────────────────────

def _extract_addr(from_header: str) -> str:
    """Extract bare email address from a From header like 'Name <addr>'."""
    m = _ADDR_RE.search(from_header)
    return m.group(1) if m else from_header.strip()


def _decode_str(header: str) -> str:
    """Decode an RFC 2047 encoded email header."""
    parts = decode_header(header)
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            result.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(text)
    return "".join(result)


def _get_body_text(msg) -> str:
    """Extract plain-text body from an email Message (handles multipart)."""
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
    """Mark an email as seen (\\Seen flag)."""
    try:
        mail.store(msg_id, "+FLAGS", "\\Seen")
    except Exception:
        logger.warning("Failed to mark message %s as seen", msg_id)
