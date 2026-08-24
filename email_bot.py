"""EmailBot — polls an IMAP inbox for video links and replies via SMTP.

Also supports cookie management commands via email:
- "更新cookie" in subject → body contains new cookie → write to .env
- "自动获取cookie" in subject → extract from browser on this machine
"""

import email
import imaplib
import json
import logging
import math
import os
import re
import smtplib
import time
from datetime import datetime
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

from backup_cleanup import BackupCleanupScheduler
from bilibili_downloader import BilibiliDownloader
from colorama import Fore, Style
from douyin_downloader import DouyinDownloader
from failure_flag import FailureFlagStore, ProcessRequestStore
from url_extractor import UrlExtractor, detect_platform

logger = logging.getLogger("EmailBot")

_ADDR_RE = re.compile(r"<([^>]+)>")
_TRANSIENT_ERROR_HINTS = ("超时", "网络连接失败", "网络", "timeout", "timed out")
_CODEX_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:EMAIL_PASSWORD|DOUYIN_COOKIE|BILIBILI_AUTH|OPENAI_API_KEY)\s*[=:]\s*[^\s]+"
)


def _format_success_reply(
    result: dict, filepath: str, prefix: str = "下载完成！"
) -> str:
    """Format a download success reply, including multi-file Bilibili results."""
    if result.get("partial"):
        prefix = "部分下载完成。"
    title = result.get("title") or "未知标题"
    lines = [prefix, f"标题：{title}", f"保存位置：{filepath}"]

    files = result.get("files") or []
    file_count = result.get("file_count") or len(files)
    if file_count > 1:
        lines.append(f"文件数量：{file_count}")
        lines.append("文件列表：")
        for path in files[:10]:
            lines.append(f"- {path}")
        if file_count > 10:
            lines.append(f"- ...另有 {file_count - 10} 个文件")

    covers = result.get("covers") or []
    if covers:
        lines.append("封面：")
        for path in covers[:5]:
            lines.append(f"- {path}")
        if len(covers) > 5:
            lines.append(f"- ...另有 {len(covers) - 5} 张封面")

    if result.get("partial"):
        failed_count = int(result.get("failed_count") or 0)
        lines.append(f"注意：有 {failed_count} 个资源下载失败，本次仅保存了上述文件。")
        for detail in (result.get("failed_items") or [])[:5]:
            lines.append(f"- {detail}")
        if result.get("retry_queued"):
            lines.append("失败资源已加入自动重试队列；已下载的文件不会重复下载。")
        if result.get("flag_status"):
            lines.append(f"失败 FLAG：{result['flag_status']}")

    return "\n".join(lines)


def _success_subject_status(result: dict, refreshed_cookie: bool = False) -> str:
    """Build a short status phrase for reply subjects."""
    if result.get("partial"):
        return f"部分成功（{result.get('file_count', 0)}个文件）"
    files = result.get("files") or []
    file_count = result.get("file_count") or len(files)
    if file_count > 1:
        return f"下载成功（{file_count}个文件）"
    if refreshed_cookie:
        return "下载成功（Cookie已刷新）"
    return "下载成功"


def _format_failure_alert(
    *,
    url: str,
    platform: str,
    subject: str,
    error_msg: str,
    attempts: int | None,
    retry_status: str,
    flag_status: str,
    pending_retry_file: Path,
    failed_links_file: Path,
) -> str:
    """Build the detailed failure message sent to the requester and owner."""
    lines = [
        "视频下载失败详细通知",
        f"时间：{_now_iso()}",
        f"平台：{platform or 'unknown'}",
        f"链接：{url}",
        f"原邮件主题：{subject or '（无主题）'}",
        f"错误：{error_msg or '未知错误'}",
        f"当前重试状态：{retry_status}",
        f"失败 FLAG 状态：{flag_status}",
    ]
    if attempts is not None:
        lines.append(f"已尝试次数：{attempts}")
    lines.extend(
        [
            f"重试队列文件：{pending_retry_file}",
            f"失败清单文件：{failed_links_file}",
            "说明：失败 FLAG 已持久化，等待宿主机 Codex 处理。",
        ]
    )
    return "\n".join(lines)


class EmailBot:
    """Monitors an inbox for emails containing supported links and downloads videos.

    Also handles cookie management commands.
    """

    def __init__(self, config):
        self.config = config
        self.downloader = DouyinDownloader(config.douyin)
        self.bilibili_downloader = BilibiliDownloader(config.bilibili)
        self.extractor = UrlExtractor()
        self._cooldowns: dict[str, float] = {}
        self._project_dir = Path(__file__).parent
        self._seen_ids: set[str] = set()  # dedup across poll cycles
        self._pending_retries: dict[str, dict] = {}
        self._failure_flags = FailureFlagStore(config.codex.flag_file)
        self._process_requests = ProcessRequestStore(config.codex.process_request_dir)
        self._pending_retry_file = Path(config.bot.transient_pending_file)
        self._failed_links_file = Path(config.bot.transient_failed_file)
        self._backup_cleanup = BackupCleanupScheduler(
            Path(config.douyin.download_path),
            retention_days=config.media_cleanup.backup_retention_days,
            check_interval_days=config.media_cleanup.check_interval_days,
        )
        self._load_pending_retries()

        # Optional .env auto-reload (for Docker: web_login writes cookie → bot picks it up)
        self._env_path = self._project_dir / ".env"
        self._env_watch = os.getenv("ENV_AUTO_RELOAD", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if self._env_watch:
            try:
                self._env_mtime: float = (
                    self._env_path.stat().st_mtime if self._env_path.exists() else 0.0
                )
            except OSError:
                self._env_mtime = 0.0
            logger.debug(".env auto-reload enabled")

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
                self._backup_cleanup.run_if_due()
                self._poll_once(cfg, bot_cfg)
            except imaplib.IMAP4.error as e:
                logger.error("IMAP error: %s — retrying in %ds", e, cfg.poll_interval)
            except smtplib.SMTPException as e:
                logger.error("SMTP error: %s", e)
            except (ConnectionError, OSError) as e:
                logger.error(
                    "Network error: %s — retrying in %ds", e, cfg.poll_interval
                )
            except Exception:
                logger.exception("Unexpected error during poll cycle")

            time.sleep(cfg.poll_interval)

    # ── Poll cycle ────────────────────────────────────────────────

    def _check_env_reload(self) -> None:
        """If .env was modified externally (e.g. by web_login), hot-reload cookie."""
        if not self._env_watch:
            return
        try:
            mtime = self._env_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._env_mtime:
            return
        self._env_mtime = mtime

        # Re-read .env
        from dotenv import load_dotenv

        load_dotenv(self._env_path, override=True)
        new_cookie = os.getenv("DOUYIN_COOKIE", "")
        if new_cookie and new_cookie != self.downloader.config.cookie:
            self.downloader.config.cookie = new_cookie
            os.environ["DOUYIN_COOKIE"] = new_cookie
            logger.info(
                "Hot-reloaded DOUYIN_COOKIE from .env (%d chars)", len(new_cookie)
            )

    def _poll_once(self, cfg, bot_cfg) -> None:
        self._check_env_reload()
        self._process_pending_retries(cfg, bot_cfg)
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
            self._safe_logout(mail)

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
        msg_id_str = (
            msg_id.decode("ascii", errors="replace")
            if isinstance(msg_id, bytes)
            else str(msg_id)
        )
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

        # ── Command: ask the host worker to process failure FLAGs ───
        if commands.codex_process and commands.codex_process in subject:
            self._handle_codex_process(mail, msg_id, cfg, sender, subject, body)
            return

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
            logger.debug(
                "Skipping — subject missing keyword '%s': %s", keyword, subject
            )
            return

        url = self.extractor.extract(subject + " " + body)
        if url is None:
            logger.info("No supported URL found from %s (subject: %s)", sender, subject)
            self._send_reply(
                cfg,
                sender,
                "未在邮件中找到支持的视频链接，请发送抖音或 B 站分享链接。",
                subject_status="未找到链接",
            )
            _mark_seen(mail, msg_id)
            return

        platform = detect_platform(url)
        logger.info(f"{Fore.CYAN}收到下载请求: %s (%s)", sender, platform or "unknown")

        # Cooldown
        now = time.time()
        if sender in self._cooldowns:
            elapsed = now - self._cooldowns[sender]
            if elapsed < bot_cfg.cooldown_seconds:
                remaining = int(bot_cfg.cooldown_seconds - elapsed)
                logger.info("Sender %s in cooldown (%ds remaining)", sender, remaining)
                return

        result = self._download_url(url)

        if result["success"]:
            partial_error = _partial_failure_error(result)
            if partial_error:
                result["flag_status"] = self._set_failure_flag(
                    cfg=cfg,
                    sender=sender,
                    subject=subject,
                    url=url,
                    platform=platform or "unknown",
                    error_msg=partial_error,
                    attempts=1,
                    retry_status=(
                        "部分资源已保存，失败资源已加入自动重试队列"
                        if _is_transient_failure(partial_error)
                        else "部分资源失败，未加入自动重试队列"
                    ),
                )
            if partial_error and _is_transient_failure(partial_error):
                self._enqueue_retry(
                    url=url,
                    sender=sender,
                    subject=subject,
                    platform=platform or "unknown",
                    error_msg=partial_error,
                    bot_cfg=bot_cfg,
                )
                result["retry_queued"] = True
            self._cooldowns[sender] = time.time()
            filepath = result["filepath"] or "未知路径"
            logger.info(
                f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载成功: %s -> %s",
                result["title"],
                filepath,
            )
            success_body = _format_success_reply(result, filepath)
            if partial_error:
                retry_status = (
                    "部分资源已保存，失败资源已加入自动重试队列"
                    if result.get("retry_queued")
                    else "部分资源失败，未加入自动重试队列"
                )
                detail_body = _format_failure_alert(
                    url=url,
                    platform=platform or "unknown",
                    subject=subject,
                    error_msg=partial_error,
                    attempts=self._pending_retries.get(
                        self._retry_key(sender, url), {}
                    ).get("attempts", 1),
                    retry_status=retry_status,
                    flag_status=result.get("flag_status", "未置位"),
                    pending_retry_file=self._pending_retry_file,
                    failed_links_file=self._failed_links_file,
                )
                self._send_failure_notifications(
                    cfg,
                    sender,
                    f"{success_body}\n\n{detail_body}",
                    subject_status="部分资源失败",
                )
            else:
                self._clear_failure_flag(sender, url)
                self._send_reply(
                    cfg,
                    sender,
                    success_body,
                    subject_status=_success_subject_status(result),
                )
        else:
            error_msg = result["error"]
            if _is_transient_failure(error_msg):
                self._enqueue_retry(
                    url=url,
                    sender=sender,
                    subject=subject,
                    platform=platform or "unknown",
                    error_msg=error_msg,
                    bot_cfg=bot_cfg,
                )
                flag_status = self._set_failure_flag(
                    cfg=cfg,
                    sender=sender,
                    subject=subject,
                    url=url,
                    platform=platform or "unknown",
                    error_msg=error_msg,
                    attempts=self._pending_retries.get(
                        self._retry_key(sender, url), {}
                    ).get("attempts"),
                    retry_status=(
                        f"已加入自动重试队列（最多 {bot_cfg.transient_retry_attempts} 次，"
                        f"间隔 {bot_cfg.transient_retry_delay_seconds} 秒）"
                    ),
                )
                body = _format_failure_alert(
                    url=url,
                    platform=platform or "unknown",
                    subject=subject,
                    error_msg=error_msg,
                    attempts=self._pending_retries.get(
                        self._retry_key(sender, url), {}
                    ).get("attempts"),
                    retry_status=(
                        f"已加入自动重试队列（最多 {bot_cfg.transient_retry_attempts} 次，"
                        f"间隔 {bot_cfg.transient_retry_delay_seconds} 秒）"
                    ),
                    flag_status=flag_status,
                    pending_retry_file=self._pending_retry_file,
                    failed_links_file=self._failed_links_file,
                )
                self._send_failure_notifications(
                    cfg, sender, body, subject_status="已加入重试"
                )
                _mark_seen(mail, msg_id)
                return

            # ── Douyin-only: auto-refresh cookie and retry once ─────
            is_cookie_issue = platform == "douyin" and (
                "删" in error_msg
                or "私密" in error_msg
                or "cookie" in error_msg.lower()
                or "异常" in error_msg
            )
            if is_cookie_issue:
                logger.info("Attempting auto cookie refresh from Firefox profile...")
                refreshed_cookie, refresh_msg = _try_extract_cookie(
                    profile_dir=self.config.cookie_extractor.profile_dir or None,
                    headless=self.config.cookie_extractor.headless,
                    validate=self.config.cookie_extractor.validate,
                )
                if (
                    refreshed_cookie
                    and refreshed_cookie != self.downloader.config.cookie
                ):
                    # Hot-reload new cookie and retry
                    self.downloader.config.cookie = refreshed_cookie
                    os.environ["DOUYIN_COOKIE"] = refreshed_cookie
                    _write_env(
                        self._project_dir / ".env", "DOUYIN_COOKIE", refreshed_cookie
                    )
                    logger.info(
                        "Cookie refreshed (%d chars → %d chars), retrying download...",
                        len(self.config.douyin.cookie),
                        len(refreshed_cookie),
                    )
                    retry_result = self.downloader.download(url)
                    if retry_result["success"]:
                        self._clear_failure_flag(sender, url)
                        self._cooldowns[sender] = time.time()
                        filepath = retry_result["filepath"] or "未知路径"
                        logger.info(
                            f"{Fore.GREEN}{Style.BRIGHT}[DONE] 下载成功 (cookie 刷新后): %s -> %s",
                            retry_result["title"],
                            filepath,
                        )
                        self._send_reply(
                            cfg,
                            sender,
                            _format_success_reply(
                                retry_result,
                                filepath,
                                prefix="下载完成！（cookie 已自动刷新）",
                            ),
                            subject_status=_success_subject_status(
                                retry_result,
                                refreshed_cookie=True,
                            ),
                        )
                        _mark_seen(mail, msg_id)
                        return
                    else:
                        logger.warning(
                            "Retry after cookie refresh also failed: %s",
                            retry_result["error"],
                        )
                        error_msg += f"\n（已尝试自动刷新 cookie 并重试，仍失败）"

            # Build helpful hints
            error_msg += (
                "\n\n解决方案："
                "\n1. 抖音链接：发送主题含「更新cookie」的邮件，正文粘贴完整 cookie"
                "\n2. 抖音链接：发送主题含「自动获取cookie」的邮件，让机器人从 Firefox 配置文件提取"
                "\n3. B站链接：如需登录内容，请在 .env 配置 BILIBILI_AUTH"
            )
            flag_status = self._set_failure_flag(
                cfg=cfg,
                sender=sender,
                subject=subject,
                url=url,
                platform=platform or "unknown",
                error_msg=error_msg,
                attempts=1,
                retry_status="未加入自动重试队列（非瞬时错误）",
            )
            body = _format_failure_alert(
                url=url,
                platform=platform or "unknown",
                subject=subject,
                error_msg=error_msg,
                attempts=1,
                retry_status="未加入自动重试队列（非瞬时错误）",
                flag_status=flag_status,
                pending_retry_file=self._pending_retry_file,
                failed_links_file=self._failed_links_file,
            )
            self._send_failure_notifications(
                cfg, sender, body, subject_status="下载失败"
            )

        _mark_seen(mail, msg_id)

    # ── Transient retry queue ─────────────────────────────────────

    def _retry_key(self, sender: str, url: str) -> str:
        return f"{sender}\n{url}"

    def _load_pending_retries(self) -> None:
        try:
            if not self._pending_retry_file.exists():
                return
            data = json.loads(self._pending_retry_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._pending_retries = {
                    str(key): value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
                logger.info(
                    "Loaded %d pending retry link(s)", len(self._pending_retries)
                )
        except Exception as exc:
            logger.warning(
                "Failed to load pending retry file %s: %s",
                self._pending_retry_file,
                exc,
            )

    def _save_pending_retries(self) -> None:
        try:
            self._pending_retry_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._pending_retry_file.with_suffix(
                self._pending_retry_file.suffix + ".tmp"
            )
            tmp_path.write_text(
                json.dumps(
                    self._pending_retries, ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self._pending_retry_file)
        except OSError as exc:
            logger.error(
                "Failed to save pending retry file %s: %s",
                self._pending_retry_file,
                exc,
            )

    def _enqueue_retry(
        self,
        url: str,
        sender: str,
        subject: str,
        platform: str,
        error_msg: str,
        bot_cfg,
    ) -> None:
        now = time.time()
        key = self._retry_key(sender, url)
        existing = self._pending_retries.get(key, {})
        attempts = _retry_attempts(existing, key) + 1
        item = {
            "url": url,
            "sender": sender,
            "subject": subject,
            "platform": platform,
            "attempts": attempts,
            "first_seen": existing.get("first_seen") or _now_iso(),
            "last_error": error_msg,
            "next_attempt_at": now + max(1, bot_cfg.transient_retry_delay_seconds),
        }
        self._pending_retries[key] = item
        self._save_pending_retries()
        logger.info(
            "Queued transient retry %d/%d for %s: %s",
            attempts,
            bot_cfg.transient_retry_attempts,
            sender,
            url,
        )

    def _process_pending_retries(self, cfg, bot_cfg) -> None:
        if not self._pending_retries:
            return

        now = time.time()
        due_keys = []
        invalid_keys = []
        for key, item in self._pending_retries.items():
            if not isinstance(item, dict):
                logger.warning(
                    "Discarding malformed pending retry %s: expected an object", key
                )
                invalid_keys.append(key)
                continue
            next_attempt_at = _retry_next_attempt_at(item, key)
            if next_attempt_at is None:
                invalid_keys.append(key)
            elif next_attempt_at <= now:
                due_keys.append(key)
        if invalid_keys:
            for key in invalid_keys:
                self._pending_retries.pop(key, None)
            self._save_pending_retries()
        if not due_keys:
            return

        for key in due_keys:
            item = self._pending_retries.get(key)
            if not item:
                continue

            url = item.get("url", "")
            sender = item.get("sender", "")
            attempts = _retry_attempts(item, key)
            # Normalize malformed values so a later retry does not encounter
            # the same bad value again.
            item["attempts"] = attempts
            logger.info(
                "Retrying transient failure %d/%d for %s: %s",
                attempts + 1,
                bot_cfg.transient_retry_attempts,
                sender,
                url,
            )
            result = self._download_url(url)
            partial_error = _partial_failure_error(result)

            if result["success"] and not partial_error:
                self._pending_retries.pop(key, None)
                self._save_pending_retries()
                self._clear_failure_flag(sender, url)
                filepath = result["filepath"] or "未知路径"
                logger.info(
                    f"{Fore.GREEN}{Style.BRIGHT}[DONE] 自动重试下载成功: %s -> %s",
                    result["title"],
                    filepath,
                )
                self._send_reply(
                    cfg,
                    sender,
                    _format_success_reply(
                        result, filepath, prefix="下载完成！（自动重试成功）"
                    ),
                    subject_status=_success_subject_status(result),
                )
                continue

            error_msg = partial_error or result.get("error") or "未知错误"
            attempts += 1
            item["attempts"] = attempts
            item["last_error"] = error_msg
            flag_status = self._set_failure_flag(
                cfg=cfg,
                sender=sender,
                subject=item.get("subject", ""),
                url=url,
                platform=item.get("platform", "unknown"),
                error_msg=error_msg,
                attempts=attempts,
                retry_status=(
                    "重试耗尽，已写入失败清单"
                    if attempts >= bot_cfg.transient_retry_attempts
                    else "仍为瞬时错误，已安排下一次重试"
                ),
            )

            if (
                attempts >= bot_cfg.transient_retry_attempts
                or not _is_transient_failure(error_msg)
            ):
                self._pending_retries.pop(key, None)
                self._save_pending_retries()
                self._record_failed_link(item, error_msg)
                retry_prefix = (
                    "自动重试后仍有部分资源未下载，已把链接保存到失败清单。"
                    if result.get("partial")
                    else "自动重试后仍未下载成功，已把链接保存到失败清单。"
                )
                body = _format_failure_alert(
                    url=url,
                    platform=item.get("platform", "unknown"),
                    subject=item.get("subject", ""),
                    error_msg=(f"{retry_prefix}\n{error_msg}"),
                    attempts=attempts,
                    retry_status="重试耗尽，已写入失败清单",
                    flag_status=flag_status,
                    pending_retry_file=self._pending_retry_file,
                    failed_links_file=self._failed_links_file,
                )
                self._send_failure_notifications(
                    cfg,
                    sender,
                    body,
                    subject_status=(
                        "部分资源重试失败" if result.get("partial") else "重试失败"
                    ),
                )
                continue

            item["next_attempt_at"] = now + max(
                1, bot_cfg.transient_retry_delay_seconds
            )
            self._pending_retries[key] = item
            self._save_pending_retries()
            logger.info(
                "Retry still transient; queued next attempt %d/%d for %s",
                attempts,
                bot_cfg.transient_retry_attempts,
                url,
            )

    def _record_failed_link(self, item: dict, error_msg: str) -> None:
        try:
            self._failed_links_file.parent.mkdir(parents=True, exist_ok=True)
            line = (
                f"{_now_iso()}\t"
                f"sender={item.get('sender', '')}\t"
                f"platform={item.get('platform', '')}\t"
                f"attempts={item.get('attempts', '')}\t"
                f"url={item.get('url', '')}\t"
                f"error={error_msg.replace(chr(9), ' ')}\n"
            )
            with self._failed_links_file.open("a", encoding="utf-8") as f:
                f.write(line)
            logger.info("Recorded failed link: %s", item.get("url", ""))
        except OSError as exc:
            logger.error(
                "Failed to record failed link in %s: %s", self._failed_links_file, exc
            )

    def _send_failure_notifications(
        self,
        cfg,
        sender: str,
        body: str,
        subject_status: str,
    ) -> None:
        """Send the detailed failure report only to the original requester."""
        body = _redact_sensitive_text(body, self.config, cfg)
        try:
            self._send_reply(cfg, sender, body, subject_status=subject_status)
        except Exception:
            logger.exception("Failed to send failure notification to %s", sender)
            raise

    def _handle_codex_process(
        self,
        mail,
        msg_id,
        cfg,
        sender: str,
        subject: str,
        body: str,
    ) -> None:
        """Queue a host-side Codex request without executing Codex in the bot."""
        url = self.extractor.extract(subject + " " + body) or ""
        try:
            request_store = getattr(self, "_process_requests", None)
            if request_store is None:
                request_store = ProcessRequestStore(
                    self.config.codex.process_request_dir
                )
                self._process_requests = request_store
            request_store.create_request(sender=sender, url=url, subject=subject)
        except Exception:
            logger.exception("Failed to persist Codex process request from %s", sender)
            self._send_reply(
                cfg,
                sender,
                "宿主机 Codex 处理请求写入失败，请稍后重试。",
                subject_status="请求失败",
            )
            _mark_seen(mail, msg_id)
            return

        scope = f"链接：{url}" if url else "将处理该发件人的全部失败记录"
        self._send_reply(
            cfg,
            sender,
            f"已请求宿主机 Codex 处理。\n{scope}",
            subject_status="已请求宿主机处理",
        )
        _mark_seen(mail, msg_id)

    def _set_failure_flag(
        self,
        *,
        cfg,
        sender: str,
        subject: str,
        url: str,
        platform: str,
        error_msg: str,
        attempts: int | None,
        retry_status: str,
    ) -> str:
        codex_cfg = getattr(getattr(self, "config", None), "codex", None)
        if codex_cfg is not None and not getattr(codex_cfg, "enabled", True):
            return "未启用"
        failure_flags = getattr(self, "_failure_flags", None)
        if failure_flags is None:
            return "未初始化"
        try:
            saved = failure_flags.set_failure(
                sender=_redact_sensitive_text(sender, self.config, cfg),
                url=_redact_sensitive_text(url, self.config, cfg),
                platform=platform,
                subject=_redact_sensitive_text(subject, self.config, cfg),
                error=_redact_sensitive_text(error_msg, self.config, cfg),
                attempts=attempts,
                retry_status=retry_status,
                key_sender=sender,
                key_url=url,
                pending_retry_file=str(self._pending_retry_file),
                failed_links_file=str(self._failed_links_file),
            )
        except Exception:
            logger.exception("Failed to persist failure FLAG for %s", url)
            return "置位失败（详见日志）"
        if not saved:
            return "置位失败（详见日志）"
        logger.info("Persisted failure FLAG for failed download: %s", url)
        return "已置位，等待宿主机 Codex 处理"

    def _clear_failure_flag(self, sender: str, url: str) -> None:
        failure_flags = getattr(self, "_failure_flags", None)
        if failure_flags is None:
            return
        try:
            if not failure_flags.clear(sender, url):
                logger.warning("Failed to clear failure FLAG for %s", url)
        except Exception:
            logger.exception("Failed to clear failure FLAG for %s", url)

    def _download_url(self, url: str) -> dict:
        """Dispatch a supported URL to the correct downloader."""
        try:
            platform = detect_platform(url)
            if platform == "douyin":
                return self.downloader.download(url)
            if platform == "bilibili":
                return self.bilibili_downloader.download(url)
            return {
                "success": False,
                "filepath": None,
                "files": [],
                "file_count": 0,
                "title": None,
                "error": "暂不支持该链接类型",
            }
        except Exception as exc:
            logger.exception("Downloader raised unexpectedly for %s", url)
            return {
                "success": False,
                "filepath": None,
                "files": [],
                "file_count": 0,
                "title": None,
                "error": f"下载器异常：{type(exc).__name__}: {exc}",
            }

    # ── Cookie command handlers ───────────────────────────────────

    def _handle_cookie_update(self, mail, msg_id, cfg, sender, body) -> None:
        """Extract new cookie from email body, write to .env, hot-reload."""
        new_cookie = body.strip()
        if not new_cookie:
            self._send_reply(
                cfg,
                sender,
                "邮件正文为空，请粘贴新的 cookie 后重试。",
                subject_status="Cookie 更新失败",
            )
            _mark_seen(mail, msg_id)
            return

        if len(new_cookie) < 100:
            logger.warning(
                "Cookie from %s looks too short (%d chars)", sender, len(new_cookie)
            )
            self._send_reply(
                cfg,
                sender,
                f"收到的 cookie 似乎不完整（仅 {len(new_cookie)} 字符），请确认已粘贴完整的 cookie 字符串。\n\n"
                "获取方式: 浏览器登录 douyin.com → F12 → 控制台 → 输入 document.cookie → 复制全部输出。",
                subject_status="Cookie 不完整",
            )
            _mark_seen(mail, msg_id)
            return

        ok = _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", new_cookie)
        if not ok:
            self._send_reply(
                cfg,
                sender,
                "写入 .env 文件失败，请检查文件权限。",
                subject_status="Cookie 写入失败",
            )
            _mark_seen(mail, msg_id)
            return

        # Hot-reload into the running downloader
        self.downloader.config.cookie = new_cookie
        os.environ["DOUYIN_COOKIE"] = new_cookie

        logger.info("Cookie updated by %s (%d chars)", sender, len(new_cookie))
        self._send_reply(
            cfg,
            sender,
            f"Cookie 已更新！（{len(new_cookie)} 字符）\n有效期通常 24-48 小时，过期后请重新发送。",
            subject_status="Cookie 已更新",
        )
        _mark_seen(mail, msg_id)

    def _handle_cookie_auto(self, mail, msg_id, cfg, sender, body) -> None:
        """Try to auto-extract cookie via headless Playwright Firefox."""
        self._send_reply(
            cfg,
            sender,
            "正在尝试自动获取 cookie（无头 Firefox）...",
            subject_status="Cookie 获取中",
        )

        cookie_str, status_msg = _try_extract_cookie(
            profile_dir=self.config.cookie_extractor.profile_dir or None,
            headless=self.config.cookie_extractor.headless,
            validate=self.config.cookie_extractor.validate,
        )

        if cookie_str:
            ok = _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", cookie_str)
            if ok:
                self.downloader.config.cookie = cookie_str
                os.environ["DOUYIN_COOKIE"] = cookie_str
                logger.info(
                    "Cookie auto-extracted (%d chars): %s", len(cookie_str), status_msg
                )
                self._send_reply(
                    cfg,
                    sender,
                    f"Cookie 已自动获取并更新！（{len(cookie_str)} 字符）\n来源：{status_msg}",
                    subject_status="Cookie 已更新",
                )
            else:
                self._send_reply(
                    cfg,
                    sender,
                    "Cookie 已提取但写入 .env 失败，请检查文件权限。",
                    subject_status="Cookie 写入失败",
                )
        else:
            self._send_reply(
                cfg,
                sender,
                f"自动获取失败：{status_msg}\n\n"
                "方案一：在宿主机终端运行 uv run python get_cookie.py\n"
                "方案二：发送主题含「更新cookie」的邮件，正文粘贴 document.cookie 的输出。",
                subject_status="Cookie 获取失败",
            )

        _mark_seen(mail, msg_id)

    # ── IMAP / SMTP helpers ───────────────────────────────────────

    def _imap_connect(self, cfg):
        logger.debug("Connecting to IMAP %s:%d", cfg.imap_server, cfg.imap_port)
        mail = imaplib.IMAP4_SSL(cfg.imap_server, cfg.imap_port)
        # Set a socket timeout so broken connections don't hang the bot.
        # 30s is enough for normal IMAP operations but prevents infinite hangs
        # when the remote side has torn down the connection (SSL EOF, timeout).
        mail.socket().settimeout(30)
        mail.login(cfg.email, cfg.password)
        return mail

    @staticmethod
    def _safe_logout(mail) -> None:
        """Close the IMAP socket directly without protocol-level LOGOUT.

        After a network error (SSL EOF, timeout), the TCP connection is
        already broken.  Calling mail.logout() would try to send LOGOUT
        and then block in recv() waiting for a server response that will
        never arrive — freezing the entire bot.

        Instead, we shut down the underlying socket at the TCP level
        (no server response needed) and let the OS clean up.
        """
        try:
            sock = mail.socket()
            # SHUT_RDWR sends TCP FIN — no protocol exchange, never blocks
            sock.shutdown(2)  # 2 = SHUT_RDWR
            sock.close()
        except Exception:
            pass

    def _send_reply(
        self,
        cfg,
        to_addr: str,
        body: str,
        subject_status: str = "通知",
    ) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = cfg.email
        msg["To"] = to_addr
        msg["Subject"] = f"Re: 视频下载 - {subject_status}"

        logger.debug("Sending reply to %s", to_addr)
        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(cfg.email, cfg.password)
            smtp.send_message(msg)

        logger.debug("Reply sent to %s", to_addr)


# ── Email parsing utilities ───────────────────────────────────────


def _redact_sensitive_text(text: str, config, cfg) -> str:
    """Remove known credentials before text reaches Codex or SMTP."""
    redacted = _CODEX_SECRET_ASSIGNMENT_RE.sub("[敏感配置已隐藏]", str(text or ""))
    values = [
        getattr(cfg, "password", ""),
        getattr(getattr(config, "douyin", None), "cookie", ""),
        getattr(getattr(config, "bilibili", None), "auth", ""),
        os.getenv("OPENAI_API_KEY", ""),
    ]
    for value in values:
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "[敏感信息已隐藏]")
    return redacted


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
    msg_str = (
        msg_id.decode("ascii", errors="replace")
        if isinstance(msg_id, bytes)
        else str(msg_id)
    )
    try:
        mail.store(msg_id, "+FLAGS", "\\Seen")
    except Exception:
        try:
            # Some IMAP servers need the flag without backslash
            mail.store(msg_id, "+FLAGS", "Seen")
        except Exception:
            logger.warning("Failed to mark message as seen: %s", msg_str)


def _is_transient_failure(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    lowered = error_msg.lower()
    return any(hint in lowered for hint in _TRANSIENT_ERROR_HINTS)


def _partial_failure_error(result: dict) -> str | None:
    """Return retryable detail for a partial result without breaking success/error semantics."""
    if not result.get("partial"):
        return None
    details = [str(item) for item in (result.get("failed_items") or []) if item]
    return "；".join(details) or None


def _retry_attempts(item: dict, key: str) -> int:
    """Read retry attempts without letting one malformed item abort polling."""
    try:
        return max(0, int(item.get("attempts", 0)))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Malformed attempts for pending retry %s; treating as zero", key)
        return 0


def _retry_next_attempt_at(item: dict, key: str) -> float | None:
    """Read a retry timestamp, returning None for malformed entries."""
    try:
        value = float(item.get("next_attempt_at", 0))
    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "Malformed next_attempt_at for pending retry %s; discarding item", key
        )
        return None
    if not math.isfinite(value):
        logger.warning(
            "Malformed next_attempt_at for pending retry %s; discarding item", key
        )
        return None
    return value


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def _try_extract_cookie(
    profile_dir: Path | None = None,
    *,
    headless: bool = True,
    validate: bool = True,
) -> tuple[str | None, str]:
    """Extract douyin.com cookies via headless Playwright Firefox.

    Args:
        profile_dir: Firefox profile directory (None = use default).
        headless: Whether Firefox should run without a visible window.
        validate: Whether to validate the extracted cookie.

    Returns:
        (cookie_string_or_None, status_message).
    """
    from cookie_extractor import extract_cookies  # noqa: E402

    return extract_cookies(
        profile_dir=profile_dir, headless=headless, validate=validate
    )
