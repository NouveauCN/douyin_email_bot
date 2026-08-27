"""EmailBot — polls an IMAP inbox for video links and replies via SMTP.

Also supports cookie management commands via email:
- "更新cookie" in subject → body contains new cookie → write to .env
- "自动获取cookie" in subject → extract from browser on this machine
"""

import email
import hashlib
import imaplib
import json
import logging
import math
import os
import re
import smtplib
import threading
import time
from datetime import datetime
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

from backup_cleanup import BackupCleanupScheduler
from bilibili_downloader import BilibiliDownloader
from colorama import Fore, Style
from douyin_downloader import DouyinDownloader
from mail_state import MailStateStore
from migrate_mail_state import import_pending_retries
from url_extractor import UrlExtractor, detect_platform

logger = logging.getLogger("EmailBot")

_ADDR_RE = re.compile(r"<([^>]+)>")
_TRANSIENT_ERROR_HINTS = ("超时", "网络连接失败", "网络", "timeout", "timed out")


def _format_success_reply(result: dict, filepath: str, prefix: str = "下载完成！") -> str:
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
        self._pending_retry_lock = threading.RLock()
        self._legacy_cleanup_pending: set[str] = set()
        self._pending_retry_file = Path(config.bot.transient_pending_file)
        self._failed_links_file = Path(config.bot.transient_failed_file)
        self._durable_mail_enabled = bool(getattr(config.bot, "durable_mail_enabled", True))
        self._state: MailStateStore | None = None
        state_db_path = Path(
            getattr(config.bot, "state_db", str(self._project_dir / "state" / "mail_state.sqlite3"))
        )
        if self._durable_mail_enabled:
            self._state = MailStateStore(
                state_db_path,
                default_lease_seconds=getattr(config.bot, "lease_seconds", 300),
            )
        elif state_db_path.exists():
            # A legacy rollback must not silently abandon Seen mail or pending
            # durable notifications. Drain the durable queues first, then
            # restart with the legacy flag.
            self._assert_legacy_rollback_safe(state_db_path, self._pending_retry_file)
        self._stop_event = threading.Event()
        self._runtime_threads: list[threading.Thread] = []
        self._platform_locks = {
            "douyin": threading.BoundedSemaphore(
                max(1, getattr(config.bot, "douyin_worker_count", 1))
            ),
            "bilibili": threading.BoundedSemaphore(
                max(1, getattr(config.bot, "bilibili_worker_count", 1))
            ),
        }
        self._cookie_lock = threading.Lock()
        self._sender_locks: dict[str, threading.Lock] = {}
        self._sender_locks_guard = threading.Lock()
        self._backup_cleanup = BackupCleanupScheduler(
            Path(config.douyin.download_path),
            retention_days=config.media_cleanup.backup_retention_days,
            check_interval_days=config.media_cleanup.check_interval_days,
        )
        self._load_pending_retries()
        if self._state is not None:
            self._migrate_legacy_retries()
            self._cleanup_terminal_legacy_mirrors()
            self._retry_legacy_cleanup()

        # Optional .env auto-reload (for Docker: web_login writes cookie → bot picks it up)
        self._env_path = self._project_dir / ".env"
        self._env_watch = os.getenv("ENV_AUTO_RELOAD", "").lower() in ("1", "true", "yes")
        if self._env_watch:
            try:
                self._env_mtime: float = self._env_path.stat().st_mtime if self._env_path.exists() else 0.0
            except OSError:
                self._env_mtime = 0.0
            logger.debug(".env auto-reload enabled")

    @staticmethod
    def _assert_legacy_rollback_safe(
        state_db_path: Path, pending_retry_path: Path | None = None
    ) -> None:
        with MailStateStore(state_db_path) as rollback_state:
            unfinished = rollback_state.unfinished_work_counts()
            terminal_legacy_keys = rollback_state.terminal_legacy_retry_keys()
        if any(unfinished.values()):
            raise RuntimeError(
                "cannot disable durable mail while SQLite work remains: "
                f"{unfinished}; drain intake/Seen/tasks/outbox before rollback"
            )
        if terminal_legacy_keys and pending_retry_path is not None:
            try:
                pending = json.loads(pending_retry_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = {}
            if isinstance(pending, dict) and terminal_legacy_keys.intersection(
                str(key) for key in pending
            ):
                raise RuntimeError(
                    "cannot disable durable mail while terminal tasks remain in "
                    "the legacy retry file"
                )

    def _cleanup_terminal_legacy_mirrors(self) -> None:
        if self._state is None:
            return
        for key in self._state.terminal_legacy_retry_keys():
            self._remove_legacy_retry({"legacy_retry_key": key})

    def run(self) -> None:
        cfg = self.config.email
        bot_cfg = self.config.bot

        logger.info(
            "EmailBot starting — mailbox: %s, poll interval: %ds",
            cfg.email,
            cfg.poll_interval,
        )

        self._backup_cleanup.run_if_due()
        self._start_runtime()
        try:
            while not self._stop_event.is_set():
                try:
                    if self._state is None:
                        self._backup_cleanup.run_if_due()
                    self._poll_once(cfg, bot_cfg)
                except imaplib.IMAP4.error as e:
                    logger.error("IMAP error: %s — retrying in %ds", e, cfg.poll_interval)
                except smtplib.SMTPException as e:
                    logger.error("SMTP error: %s", e)
                except (ConnectionError, OSError) as e:
                    logger.error("Network error: %s — retrying in %ds", e, cfg.poll_interval)
                except Exception:
                    logger.exception("Unexpected error during poll cycle")
                self._stop_event.wait(cfg.poll_interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop intake and let bounded runtime threads finish their current work."""
        self._stop_event.set()
        for thread in self._runtime_threads:
            thread.join(timeout=10)
        self._runtime_threads.clear()
        if self._state is not None:
            self._state.close()

    def _start_runtime(self) -> None:
        if self._state is None or self._runtime_threads:
            return
        worker_count = max(1, getattr(self.config.bot, "worker_count", 2))
        for index in range(worker_count):
            thread = threading.Thread(
                target=self._task_worker_loop,
                args=(f"download-{index + 1}",),
                name=f"mail-download-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._runtime_threads.append(thread)
        outbox = threading.Thread(
            target=self._outbox_worker_loop,
            name="mail-smtp-outbox",
            daemon=True,
        )
        outbox.start()
        self._runtime_threads.append(outbox)
        maintenance = threading.Thread(
            target=self._maintenance_loop,
            name="mail-maintenance",
            daemon=True,
        )
        maintenance.start()
        self._runtime_threads.append(maintenance)

    def _maintenance_loop(self) -> None:
        """Recover leases and run retries/cleanup independently of IMAP intake."""
        interval = max(1, min(30, getattr(self.config.email, "poll_interval", 30)))
        while not self._stop_event.wait(interval):
            try:
                if self._state is not None:
                    recovered = self._state.recover_expired()
                    if recovered["tasks"] or recovered["outbox"]:
                        logger.warning("Recovered expired mail leases: %s", recovered)
                    self._retry_legacy_cleanup()
                self._backup_cleanup.run_if_due()
                if self._state is None:
                    self._process_pending_retries(self.config.email, self.config.bot)
            except Exception:
                logger.exception("Independent mail maintenance failed")

    def _migrate_legacy_retries(self) -> None:
        """Import JSON retry entries without deleting the rollback source."""
        if self._state is None:
            return
        try:
            report = import_pending_retries(self._pending_retry_file, self._state)
            if report["imported"]:
                logger.info("Imported %d legacy retry record(s) into SQLite", report["imported"])
            if report["skipped"]:
                logger.warning("Skipped %d malformed legacy retry record(s)", report["skipped"])
        except Exception:
            logger.exception("Could not import legacy retry file %s", self._pending_retry_file)

    def _task_worker_loop(self, worker_id: str) -> None:
        assert self._state is not None
        while not self._stop_event.is_set():
            try:
                tasks = self._state.claim_tasks(
                    1,
                    lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                    worker_id=worker_id,
                )
                if not tasks:
                    self._stop_event.wait(0.5)
                    continue
                for task in tasks:
                    self._process_durable_task(task)
            except Exception:
                logger.exception("Durable download worker %s failed", worker_id)
                self._stop_event.wait(1)

    def _process_durable_task(self, task: dict) -> None:
        assert self._state is not None
        task_id = int(task["id"])
        token = task["lease_token"]
        platform = task.get("platform") or detect_platform(task.get("original_url", ""))
        lock = self._platform_locks.get(platform)
        # Do not hold a lease while waiting for another task of the same
        # platform. The task is returned to the queue and can be claimed when
        # that bounded capacity becomes available.
        acquired = lock.acquire(blocking=False) if lock else True
        if not acquired:
            self._state.release_task(
                task_id,
                token,
                next_attempt_at=time.time() + 1,
            )
            return

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_task,
            args=(task_id, token, heartbeat_stop),
            name=f"mail-heartbeat-{task_id}",
            daemon=True,
        )
        heartbeat.start()
        sender = str((task.get("payload") or {}).get("sender") or "")
        sender_lock = self._sender_lock(sender)
        sender_lock.acquire()
        try:
            if sender:
                remaining = getattr(self.config.bot, "cooldown_seconds", 0) - (
                    time.time() - self._cooldowns.get(sender, 0)
                )
                if remaining > 0:
                    getattr(self, "_stop_event", threading.Event()).wait(remaining)
            if platform == "notice":
                # Notice tasks are normally completed during intake. This
                # branch closes the crash window between their enqueue and
                # the atomic task/outbox transition.
                notification = (task.get("payload") or {}).get("notification")
                if notification:
                    self._state.complete_task(
                        task_id,
                        token,
                        result={"notice": (task.get("payload") or {}).get("event")},
                        notification=notification,
                        outbox_event=(task.get("payload") or {}).get("event", "notice"),
                    )
                else:
                    self._complete_durable_failure(task, token, "notice payload missing")
                return
            if platform == "cookie":
                notification, safe_payload = self._process_cookie_command(task)
                completed = self._state.complete_task(
                    task_id,
                    token,
                    result={"command": task.get("payload", {}).get("command")},
                    notification=notification,
                )
                if completed is not None:
                    self._state.redact_task_payload(task_id, safe_payload)
                return
            result = self._download_url(task["original_url"])
            if (
                not result.get("success")
                and platform == "douyin"
                and _is_cookie_failure(result.get("error"))
            ):
                result = self._retry_douyin_after_cookie_refresh(task["original_url"], result)
            partial_error = _partial_failure_error(result)
            error_msg = partial_error or result.get("error")
            if result.get("success") and partial_error:
                if _is_transient_failure(partial_error):
                    attempts = int(task.get("attempts") or 1)
                    if attempts < self.config.bot.transient_retry_attempts:
                        self._state.fail_task(
                            task_id,
                            token,
                            partial_error,
                            retry_at=time.time() + max(1, self.config.bot.transient_retry_delay_seconds),
                        )
                        return
                self._complete_durable_failure(task, token, partial_error, result)
                return
            if result.get("success"):
                if sender:
                    self._cooldowns[sender] = time.time()
                filepath = result.get("filepath") or "未知路径"
                payload = {
                    "to_addr": task.get("payload", {}).get("sender", ""),
                    "body": _format_success_reply(result, filepath),
                    "subject_status": _success_subject_status(result),
                }
                completed = self._state.complete_task(
                    task_id,
                    token,
                    result=result,
                    notification=payload,
                )
                if completed is None:
                    logger.warning("Lost lease before completing task %d", task_id)
                else:
                    self._remove_legacy_retry(task.get("payload", {}))
                return

            if error_msg and _is_transient_failure(error_msg):
                attempts = int(task.get("attempts") or 1)
                if attempts < self.config.bot.transient_retry_attempts:
                    self._state.fail_task(
                        task_id,
                        token,
                        error_msg,
                        retry_at=time.time() + max(1, self.config.bot.transient_retry_delay_seconds),
                    )
                    return

            self._complete_durable_failure(task, token, error_msg or "未知错误")
        except Exception as exc:
            logger.exception("Durable task %d failed", task_id)
            attempts = int(task.get("attempts") or 1)
            if attempts < self.config.bot.transient_retry_attempts:
                self._state.fail_task(
                    task_id,
                    token,
                    str(exc),
                    retry_at=time.time() + max(1, self.config.bot.transient_retry_delay_seconds),
                )
            else:
                self._complete_durable_failure(task, token, str(exc))
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            if lock:
                lock.release()
            sender_lock.release()

    def _sender_lock(self, sender: str) -> threading.Lock:
        with self._sender_locks_guard:
            return self._sender_locks.setdefault(sender, threading.Lock())

    def _process_cookie_command(self, task: dict) -> tuple[dict, dict]:
        """Execute a durable cookie command and return a safe outbox payload."""
        payload = task.get("payload") or {}
        sender = payload.get("sender", "")
        command = payload.get("command")
        body = str(payload.get("body") or "").strip()
        safe_payload = {
            "sender": sender,
            "subject": payload.get("subject", ""),
            "command": command,
        }
        if command == "cookie_update":
            if not body:
                return {
                    "to_addr": sender,
                    "body": "邮件正文为空，请粘贴新的 cookie 后重试。",
                    "subject_status": "Cookie 更新失败",
                }, safe_payload
            if len(body) < 100:
                return {
                    "to_addr": sender,
                    "body": (
                        f"收到的 cookie 似乎不完整（仅 {len(body)} 字符），请确认已粘贴完整的 cookie 字符串。\n\n"
                        "获取方式: 浏览器登录 douyin.com → F12 → 控制台 → 输入 document.cookie → 复制全部输出。"
                    ),
                    "subject_status": "Cookie 不完整",
                }, safe_payload
            if not _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", body):
                return {
                    "to_addr": sender,
                    "body": "写入 .env 文件失败，请检查文件权限。",
                    "subject_status": "Cookie 写入失败",
                }, safe_payload
            with self._cookie_lock:
                self.downloader.config.cookie = body
                os.environ["DOUYIN_COOKIE"] = body
            return {
                "to_addr": sender,
                "body": f"Cookie 已更新！（{len(body)} 字符）\n有效期通常 24-48 小时，过期后请重新发送。",
                "subject_status": "Cookie 已更新",
            }, safe_payload

        if command == "cookie_auto":
            with self._cookie_lock:
                cookie, status = _try_extract_cookie(
                    profile_dir=self.config.cookie_extractor.profile_dir or None,
                    headless=self.config.cookie_extractor.headless,
                    validate=self.config.cookie_extractor.validate,
                )
                if cookie and _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", cookie):
                    self.downloader.config.cookie = cookie
                    os.environ["DOUYIN_COOKIE"] = cookie
                    return {
                        "to_addr": sender,
                        "body": f"Cookie 已自动获取并更新！（{len(cookie)} 字符）\n来源：{status}",
                        "subject_status": "Cookie 已更新",
                    }, safe_payload
            return {
                "to_addr": sender,
                "body": (
                    f"自动获取失败：{status}\n\n"
                    "方案一：在宿主机终端运行 uv run python get_cookie.py\n"
                    "方案二：发送主题含「更新cookie」的邮件，正文粘贴 document.cookie 的输出。"
                ),
                "subject_status": "Cookie 获取失败",
            }, safe_payload

        return {
            "to_addr": sender,
            "body": "不支持的 cookie 命令。",
            "subject_status": "Cookie 操作失败",
        }, safe_payload

    def _complete_durable_failure(
        self,
        task: dict,
        token: str,
        error: str,
        result: dict | None = None,
    ) -> None:
        """Terminally fail a task and persist its user notification atomically."""
        assert self._state is not None
        payload = task.get("payload") or {}
        sender = payload.get("sender", "")
        if result and result.get("success"):
            filepath = result.get("filepath") or "未知路径"
            body = _format_success_reply(
                result,
                filepath,
                prefix="部分下载完成，但自动重试已耗尽。",
            ) + f"\n最后错误：{error}"
            subject_status = "部分下载失败"
        else:
            body = f"下载失败：{error}"
            subject_status = "下载失败"
        self._record_failed_link(
            {
                "sender": sender,
                "platform": task.get("platform", ""),
                "attempts": task.get("attempts", ""),
                "url": task.get("original_url", ""),
            },
            error,
        )
        failed = self._state.fail_task(
            int(task["id"]),
            token,
            error,
            notification={
                "to_addr": sender,
                "body": body,
                "subject_status": subject_status,
            },
            outbox_event="partial-failed" if result and result.get("success") else "failed",
        )
        if failed is not None:
            # The durable terminal failure and its notification are now safe;
            # remove the mirrored legacy retry so a later rollback cannot
            # execute the same link a second time.
            self._remove_legacy_retry(payload)

    def _heartbeat_task(self, task_id: int, token: str, stop: threading.Event) -> None:
        assert self._state is not None
        interval = max(1, getattr(self.config.bot, "heartbeat_seconds", 30))
        while not stop.wait(interval):
            try:
                if self._state.heartbeat_task(
                    task_id,
                    token,
                    lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                ) is None:
                    return
            except Exception:
                logger.exception("Task heartbeat failed for %d", task_id)

    def _retry_douyin_after_cookie_refresh(self, url: str, first_result: dict) -> dict:
        """Serialize Firefox access and preserve the existing cookie retry."""
        with self._cookie_lock:
            refreshed_cookie, refresh_msg = _try_extract_cookie(
                profile_dir=self.config.cookie_extractor.profile_dir or None,
                headless=self.config.cookie_extractor.headless,
                validate=self.config.cookie_extractor.validate,
            )
            if not refreshed_cookie or refreshed_cookie == self.downloader.config.cookie:
                return first_result
            old_length = len(self.downloader.config.cookie)
            self.downloader.config.cookie = refreshed_cookie
            os.environ["DOUYIN_COOKIE"] = refreshed_cookie
            _write_env(self._project_dir / ".env", "DOUYIN_COOKIE", refreshed_cookie)
            logger.info(
                "Cookie refreshed (%d chars -> %d chars; %s), retrying durable task",
                old_length,
                len(refreshed_cookie),
                refresh_msg,
            )
            return self.downloader.download(url)

    def _outbox_worker_loop(self) -> None:
        assert self._state is not None
        while not self._stop_event.is_set():
            try:
                items = self._state.claim_outbox(
                    1,
                    lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                    worker_id="smtp-outbox",
                )
                if not items:
                    self._stop_event.wait(0.5)
                    continue
                for item in items:
                    self._deliver_outbox(item)
            except Exception:
                logger.exception("SMTP outbox worker failed")
                self._stop_event.wait(1)

    def _deliver_outbox(self, item: dict) -> None:
        assert self._state is not None
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_outbox,
            args=(int(item["id"]), item["lease_token"], heartbeat_stop),
            name=f"mail-outbox-heartbeat-{item['id']}",
            daemon=True,
        )
        heartbeat.start()
        try:
            payload = item.get("payload") or {}
            self._send_reply(
                self.config.email,
                payload.get("to_addr", ""),
                payload.get("body", ""),
                subject_status=payload.get("subject_status", "通知"),
                message_id=item.get("message_id"),
            )
        except Exception as exc:
            attempts = int(item.get("attempts") or 1)
            max_attempts = max(1, getattr(self.config.bot, "outbox_retry_attempts", 5))
            retry_at = None
            if attempts < max_attempts:
                retry_at = time.time() + max(1, getattr(self.config.bot, "outbox_retry_delay_seconds", 60))
            self._state.mark_outbox_failed(
                int(item["id"]),
                item["lease_token"],
                str(exc),
                retry_at=retry_at,
            )
            logger.warning("SMTP outbox %d delivery failed (attempt %d/%d): %s", item["id"], attempts, max_attempts, exc)
        else:
            self._state.mark_outbox_sent(int(item["id"]), item["lease_token"])
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def _heartbeat_outbox(self, outbox_id: int, token: str, stop: threading.Event) -> None:
        assert self._state is not None
        interval = max(1, getattr(self.config.bot, "heartbeat_seconds", 30))
        while not stop.wait(interval):
            try:
                if self._state.heartbeat_outbox(
                    outbox_id,
                    token,
                    lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                ) is None:
                    return
            except Exception:
                logger.exception("SMTP outbox heartbeat failed for %d", outbox_id)

    def _retry_legacy_cleanup(self) -> None:
        for key in list(self._legacy_cleanup_pending):
            self._remove_legacy_retry({"legacy_retry_key": key})

    def _remove_legacy_retry(self, payload: dict) -> bool:
        key = payload.get("legacy_retry_key")
        if not key:
            return False
        with self._pending_retry_lock:
            if key not in self._pending_retries:
                self._legacy_cleanup_pending.discard(key)
                return False
            # The task completion transaction already made the notification
            # durable. Remove the mirrored retry only if the atomic JSON
            # replacement succeeds; otherwise restore it for maintenance.
            original = self._pending_retries.pop(key)
            if self._save_pending_retries():
                self._legacy_cleanup_pending.discard(key)
                return True
            self._pending_retries[key] = original
            self._legacy_cleanup_pending.add(key)
            logger.error("Could not persist removal of legacy retry %s; will retry", key)
            return False

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
        if self._state is not None:
            self._poll_once_durable(cfg, bot_cfg)
            return
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

    def _poll_once_durable(self, cfg, bot_cfg) -> None:
        """Fetch UIDs and durably accept them before acknowledging ``\\Seen``."""
        assert self._state is not None
        mailbox = "INBOX"
        mail = self._imap_connect(cfg)
        try:
            mail.select(mailbox)
            uidvalidity = self._imap_uidvalidity(mail)
            if uidvalidity is None:
                logger.error("IMAP UIDVALIDITY is unavailable; refusing sequence-based intake")
                return
            position = self._state.get_mailbox_position(mailbox)
            reset = position is None or position["uidvalidity"] != uidvalidity
            start_uid = 1 if reset else int(position["last_uid"]) + 1
            if reset:
                # Establish the new UID epoch even when the mailbox is empty;
                # otherwise every empty reconciliation would repeat the reset.
                self._state.set_mailbox_position(mailbox, uidvalidity, 0)

            # Re-acknowledge messages whose intake commit succeeded but whose
            # IMAP STORE failed in a previous cycle.
            self._retry_pending_seen(mail, mailbox, uidvalidity)

            uids: set[int] = set()
            # Re-fetch sources whose routing marker is still incomplete even
            # if an external client changed their Seen flag after a crash.
            uids.update(
                int(item["uid"])
                for item in self._state.pending_intake(mailbox, uidvalidity)
            )
            # Reconcile the durable high-water mark and still honor the
            # service's normal UNSEEN poll contract. The union also catches a
            # message whose Seen flag was changed outside this bot.
            for criteria in (f"UID {start_uid}:*", "UNSEEN"):
                status, data = mail.uid("search", None, criteria)
                if status != "OK":
                    logger.warning("IMAP UID SEARCH failed for %s", criteria)
                    return
                uid_bytes = data[0] if data and data[0] else b""
                for value in uid_bytes.split():
                    if not value.isdigit():
                        continue
                    parsed_uid = int(value)
                    # Some servers return the current last UID for an empty
                    # range such as ``UID 1000:*``. Never let that regress
                    # the durable high-water mark; UNSEEN remains independent
                    # and still catches historical flag changes.
                    if criteria.startswith("UID ") and parsed_uid < start_uid:
                        continue
                    uids.add(parsed_uid)
            uids = sorted(uids)
            for uid in uids:
                fetched = self._imap_fetch_uid(mail, uid)
                if fetched is None:
                    # Do not advance beyond an un-fetchable UID. The next
                    # reconciliation will retry it instead of silently
                    # skipping a message.
                    logger.warning("Could not fetch IMAP UID %d; stopping reconciliation", uid)
                    break
                source_id = f"{mailbox}:{uidvalidity}:{uid}"
                try:
                    accepted = self._durably_accept_email(
                        mail, mailbox, uidvalidity, uid, source_id, fetched, cfg, bot_cfg
                    )
                except Exception as exc:
                    logger.exception(
                        "Durable routing failed for UID %d; quarantining message",
                        uid,
                    )
                    accepted = self._quarantine_durable_email(
                        mail,
                        mailbox,
                        uidvalidity,
                        uid,
                        source_id,
                        fetched,
                        exc,
                    )
                if not accepted:
                    break
        finally:
            self._safe_logout(mail)

    @staticmethod
    def _imap_uidvalidity(mail) -> int | None:
        try:
            _status, values = mail.response("UIDVALIDITY")
            if values:
                value = values[-1]
                if isinstance(value, bytes):
                    value = value.split()[-1]
                parsed = int(value)
                return parsed if parsed > 0 else None
        except (AttributeError, TypeError, ValueError, imaplib.IMAP4.error):
            pass
        return None

    @staticmethod
    def _imap_fetch_uid(mail, uid: int):
        try:
            status, data = mail.uid("fetch", str(uid), "(UID BODY.PEEK[])")
        except (AttributeError, imaplib.IMAP4.error, OSError):
            return None
        if status != "OK" or not data:
            return None
        for part in data:
            if (
                isinstance(part, tuple)
                and len(part) > 1
                and isinstance(part[1], bytes)
                and _uid_matches_fetch_metadata(part[0], uid)
                and _looks_like_rfc822(part[1])
            ):
                return part[1]
        return None

    def _retry_pending_seen(self, mail, mailbox: str, uidvalidity: int) -> None:
        assert self._state is not None
        for item in self._state.pending_seen(mailbox):
            if int(item["uidvalidity"]) != uidvalidity:
                continue
            try:
                ok = _mark_seen_uid(mail, int(item["uid"]))
            except Exception:
                ok = False
            if ok:
                self._state.ack_message(item["source_message_id"])

    def _durably_accept_email(
        self,
        mail,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        source_id: str,
        raw: bytes,
        cfg,
        bot_cfg,
    ) -> bool:
        """Commit source identity and route work before attempting IMAP STORE."""
        assert self._state is not None
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        urls: list[str] = []
        platform = None
        command = None
        routing_error = None
        try:
            msg = email.message_from_bytes(raw)
            sender = _extract_addr(msg.get("From", ""))
            subject = _decode_str(msg.get("Subject", ""))
            if not sender:
                sender = "unknown-sender"
            body = _get_body_text(msg)
            metadata = {
                "sender": sender,
                "subject": subject,
                "message_id": msg.get("Message-ID", ""),
                "raw_sha256": raw_sha256,
            }

            sender_allowed = not bot_cfg.allowed_senders or sender in bot_cfg.allowed_senders
            if sender != cfg.email and sender_allowed:
                if bot_cfg.commands.cookie_update and bot_cfg.commands.cookie_update in subject:
                    command = "cookie_update"
                elif bot_cfg.commands.cookie_auto and bot_cfg.commands.cookie_auto in subject:
                    command = "cookie_auto"
                else:
                    url = self.extractor.extract(subject + " " + body)
                    if url:
                        urls = [url]
                        platform = detect_platform(url)
        except Exception as exc:
            routing_error = type(exc).__name__
            logger.warning(
                "Safely isolating durable mail UID %d after %s during parsing/routing",
                uid,
                routing_error,
            )
            sender = None
            body = ""
            metadata = {
                "uid": uid,
                "uidvalidity": uidvalidity,
                "raw_sha256": raw_sha256,
                "intake_error": routing_error,
            }

        try:
            accepted = self._state.accept_message(
                mailbox,
                uidvalidity,
                uid,
                source_id,
                urls,
                metadata=metadata
                if routing_error
                else {**metadata, "platform": platform},
                platform=platform,
            )
        except Exception:
            logger.exception("Durable intake failed for UID %d", uid)
            return False

        routing_pending = not accepted["intake_complete"]
        if accepted["duplicate"]:
            logger.debug("UID %d was already durably accepted", uid)
        if routing_pending:
            if routing_error:
                # A fetched message must not block the UID reconciliation
                # loop merely because its content could not be routed. Keep
                # only the safe quarantine metadata above.
                pass
            elif urls:
                logger.info(
                    "Durably accepted mail UID %d with %s task",
                    uid,
                    platform or "unknown",
                )
            elif command:
                self._ensure_command_task(source_id, metadata, body, command)

            # A source stays pending until URL/command routing (or the safe
            # empty/quarantine intake) has been persisted.
            try:
                if not self._state.mark_intake_complete(source_id):
                    logger.warning("Could not finalize durable intake for UID %d", uid)
                    return False
            except Exception:
                logger.exception("Could not finalize durable intake for UID %d", uid)
                return False

        try:
            if _mark_seen_uid(mail, uid):
                self._state.ack_message(source_id)
        except Exception:
            logger.warning("Durable intake committed but IMAP \\Seen ACK failed for UID %d", uid)
        return True

    def _quarantine_durable_email(
        self,
        mail,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        source_id: str,
        raw: bytes,
        error: BaseException,
    ) -> bool:
        """Persist safe error metadata, acknowledge, and isolate one mail."""
        assert self._state is not None
        metadata = {
            "uid": uid,
            "uidvalidity": uidvalidity,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "intake_error": type(error).__name__,
        }
        try:
            accepted = self._state.accept_message(
                mailbox,
                uidvalidity,
                uid,
                source_id,
                [],
                metadata=metadata,
            )
            if not self._state.replace_source_metadata(source_id, metadata):
                logger.error("Could not persist quarantine metadata for UID %d", uid)
                return False
            if (
                not accepted["intake_complete"]
                and not self._state.mark_intake_complete(source_id)
            ):
                logger.error("Could not finalize quarantined intake for UID %d", uid)
                return False
            if _mark_seen_uid(mail, uid):
                self._state.ack_message(source_id)
            return True
        except Exception:
            logger.exception("Could not quarantine malformed email UID %d", uid)
            return False

    def _ensure_command_task(
        self, source_id: str, metadata: dict, body: str, command: str
    ) -> int:
        assert self._state is not None
        notice_url = f"urn:mail-notice:{command}"
        existing = self._state.get_task(source_id, notice_url)
        if existing is not None:
            # A duplicate UID after a crash may reach this path again. The
            # notice/outbox transaction is idempotent, so do not re-run a
            # cookie side effect or send a second notification.
            return int(existing["id"])
        # Cookie input is deliberately processed in memory: durable state
        # must not become a second plaintext cookie store. Only the resulting
        # safe notification is persisted in the SMTP outbox.
        notification, _safe_payload = self._process_cookie_command(
            {"payload": {**metadata, "body": body, "command": command}}
        )
        return self._ensure_task_for_notice(
            source_id,
            metadata,
            command,
            notification,
        )

    def _ensure_task_for_notice(
        self, source_id: str, metadata: dict, event: str, notification: dict
    ) -> int:
        assert self._state is not None
        task = self._state.enqueue_notice(
            source_id,
            event,
            {**metadata, "notification": notification, "event": event},
            notification,
        )
        return int(task["id"])

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
        url = self.extractor.extract(subject + " " + body)
        if url is None:
            logger.debug("No supported URL found from %s (subject: %s)", sender, subject)
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
            self._send_reply(
                cfg, sender,
                _format_success_reply(result, filepath),
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
                self._send_reply(
                    cfg,
                    sender,
                    (
                        "下载暂时失败，已加入自动重试队列。\n"
                        f"原因：{error_msg}\n"
                        f"最多尝试：{bot_cfg.transient_retry_attempts} 次\n"
                        f"重试间隔：{bot_cfg.transient_retry_delay_seconds} 秒"
                    ),
                    subject_status="已加入重试",
                )
                _mark_seen(mail, msg_id)
                return

            # ── Douyin-only: auto-refresh cookie and retry once ─────
            is_cookie_issue = (
                platform == "douyin"
                and (
                "删" in error_msg
                or "私密" in error_msg
                or "cookie" in error_msg.lower()
                or "异常" in error_msg
                )
            )
            if is_cookie_issue:
                logger.info("Attempting auto cookie refresh from Firefox profile...")
                refreshed_cookie, refresh_msg = _try_extract_cookie(
                    profile_dir=self.config.cookie_extractor.profile_dir or None,
                    headless=self.config.cookie_extractor.headless,
                    validate=self.config.cookie_extractor.validate,
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
                        logger.warning("Retry after cookie refresh also failed: %s", retry_result["error"])
                        error_msg += f"\n（已尝试自动刷新 cookie 并重试，仍失败）"

            # Build helpful hints
            error_msg += (
                "\n\n解决方案："
                "\n1. 抖音链接：发送主题含「更新cookie」的邮件，正文粘贴完整 cookie"
                "\n2. 抖音链接：发送主题含「自动获取cookie」的邮件，让机器人从 Firefox 配置文件提取"
                "\n3. B站链接：如需登录内容，请在 .env 配置 BILIBILI_AUTH"
            )
            self._send_reply(
                cfg,
                sender,
                f"下载失败：{error_msg}",
                subject_status="下载失败",
            )

        _mark_seen(mail, msg_id)

    # ── Transient retry queue ─────────────────────────────────────

    def _retry_key(self, sender: str, url: str) -> str:
        return f"{sender}\n{url}"

    def _load_pending_retries(self) -> None:
        with self._pending_retry_lock:
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
                    logger.info("Loaded %d pending retry link(s)", len(self._pending_retries))
            except Exception as exc:
                logger.warning("Failed to load pending retry file %s: %s", self._pending_retry_file, exc)

    def _save_pending_retries(self) -> bool:
        with self._pending_retry_lock:
            try:
                self._pending_retry_file.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = self._pending_retry_file.with_suffix(self._pending_retry_file.suffix + ".tmp")
                tmp_path.write_text(
                    json.dumps(self._pending_retries, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                tmp_path.replace(self._pending_retry_file)
                return True
            except OSError as exc:
                logger.error("Failed to save pending retry file %s: %s", self._pending_retry_file, exc)
                return False

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
                logger.warning("Discarding malformed pending retry %s: expected an object", key)
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
                filepath = result["filepath"] or "未知路径"
                logger.info(
                    f"{Fore.GREEN}{Style.BRIGHT}[DONE] 自动重试下载成功: %s -> %s",
                    result["title"],
                    filepath,
                )
                self._send_reply(
                    cfg,
                    sender,
                    _format_success_reply(result, filepath, prefix="下载完成！（自动重试成功）"),
                    subject_status=_success_subject_status(result),
                )
                continue

            error_msg = partial_error or result.get("error") or "未知错误"
            attempts += 1
            item["attempts"] = attempts
            item["last_error"] = error_msg

            if attempts >= bot_cfg.transient_retry_attempts or not _is_transient_failure(error_msg):
                self._pending_retries.pop(key, None)
                self._save_pending_retries()
                self._record_failed_link(item, error_msg)
                retry_prefix = (
                    "自动重试后仍有部分资源未下载，已把链接保存到失败清单。"
                    if result.get("partial") else
                    "自动重试后仍未下载成功，已把链接保存到失败清单。"
                )
                self._send_reply(
                    cfg,
                    sender,
                    (
                        f"{retry_prefix}\n"
                        f"链接：{url}\n"
                        f"失败清单：{self._failed_links_file}\n"
                        f"最后错误：{error_msg}"
                    ),
                    subject_status="部分资源重试失败" if result.get("partial") else "重试失败",
                )
                continue

            item["next_attempt_at"] = now + max(1, bot_cfg.transient_retry_delay_seconds)
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
            logger.error("Failed to record failed link in %s: %s", self._failed_links_file, exc)

    def _download_url(self, url: str) -> dict:
        """Dispatch a supported URL to the correct downloader."""
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
            logger.warning("Cookie from %s looks too short (%d chars)", sender, len(new_cookie))
            self._send_reply(
                cfg, sender,
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
            cfg, sender,
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
                logger.info("Cookie auto-extracted (%d chars): %s", len(cookie_str), status_msg)
                self._send_reply(
                    cfg, sender,
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
                cfg, sender,
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
        message_id: str | None = None,
    ) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = cfg.email
        msg["To"] = to_addr
        msg["Subject"] = f"Re: 视频下载 - {subject_status}"
        if message_id:
            msg["Message-ID"] = message_id

        logger.debug("Sending reply to %s", to_addr)
        with smtplib.SMTP(
            cfg.smtp_server,
            cfg.smtp_port,
            timeout=max(1, getattr(cfg, "smtp_timeout", 30)),
        ) as smtp:
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
            result.append(_decode_bytes(text, charset))
        else:
            result.append(text)
    return "".join(result)


def _decode_bytes(value: bytes, charset: str | None = None) -> str:
    """Decode mail text safely, falling back for unknown charset labels."""
    try:
        return value.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError, ValueError):
        return value.decode("utf-8", errors="replace")


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
                    parts.append(_decode_bytes(payload, charset))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return _decode_bytes(payload, charset)
    return ""


def _looks_like_rfc822(raw: bytes) -> bool:
    """Reject IMAP protocol metadata accidentally returned as message data."""
    if not isinstance(raw, bytes):
        return False
    header_block = raw.split(b"\r\n\r\n", 1)[0]
    if header_block == raw:
        header_block = raw.split(b"\n\n", 1)[0]
    if not header_block or len(header_block) == len(raw) and b"\n" not in raw:
        return False
    return any(
        re.match(rb"^[A-Za-z0-9-]+:", line) is not None
        for line in header_block.splitlines()
    )


def _uid_matches_fetch_metadata(metadata, requested_uid: int) -> bool:
    if isinstance(metadata, str):
        metadata = metadata.encode("ascii", errors="ignore")
    if not isinstance(metadata, bytes):
        return False
    match = re.search(rb"\bUID\s+([0-9]+)\b", metadata, flags=re.IGNORECASE)
    if match is None:
        return False
    try:
        return int(match.group(1)) == requested_uid
    except (TypeError, ValueError):
        return False


def _mark_seen_uid(mail, uid: int) -> bool:
    """Mark an IMAP message by UID; never fall back to sequence numbers."""
    uid_str = str(uid)
    try:
        result = mail.uid("store", uid_str, "+FLAGS", "\\Seen")
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and result[0] in ("OK", b"OK")
        ):
            return True
    except Exception:
        pass
    logger.warning("Failed to mark UID as seen: %s", uid_str)
    return False


def _mark_seen(mail, msg_id: bytes) -> bool:
    """Mark an email as read. Tries two methods for compatibility."""
    msg_str = msg_id.decode("ascii", errors="replace") if isinstance(msg_id, bytes) else str(msg_id)
    for flag in ("\\Seen", "Seen"):
        try:
            result = mail.store(msg_id, "+FLAGS", flag)
            if not isinstance(result, tuple) or result[0] == "OK":
                return True
        except Exception:
            continue
    logger.warning("Failed to mark message as seen: %s", msg_str)
    return False


def _is_transient_failure(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    lowered = error_msg.lower()
    return any(hint in lowered for hint in _TRANSIENT_ERROR_HINTS)


def _is_cookie_failure(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    lowered = error_msg.lower()
    return any(hint in lowered for hint in ("删", "私密", "cookie", "异常"))


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
        logger.warning("Malformed next_attempt_at for pending retry %s; discarding item", key)
        return None
    if not math.isfinite(value):
        logger.warning("Malformed next_attempt_at for pending retry %s; discarding item", key)
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

    return extract_cookies(profile_dir=profile_dir, headless=headless, validate=validate)
