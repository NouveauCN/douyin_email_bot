"""EmailBot — IMAP intake adapter and email notification projector.

Downloads execute through the shared ``DownloadTaskService`` in durable mode;
the legacy mode uses its synchronous ``DownloadExecutor`` compatibility path.
Cookie acquisition is intentionally provided by Web Login and ``get_cookie.py``
instead of email commands.
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
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

from backup_cleanup import BackupCleanupScheduler
from bilibili_downloader import BilibiliDownloader
from colorama import Fore, Style
from douyin_downloader import DouyinDownloader
from download_task_service import (
    DownloadExecutor,
    DownloaderRegistry,
    DownloadTaskService,
)
from mail_state import MailStateStore
from migrate_mail_state import import_pending_retries
from settings_store import SettingsStore, default_database_path
from task_store import TaskStore
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


def _download_failure_hint() -> str:
    return (
        "\n\n解决方案："
        "\n1. 抖音链接：通过 Web Login 更新托管 Cookie；也可运行 uv run python get_cookie.py"
        "\n2. B站链接：如需登录内容，请在 .env 配置 BILIBILI_AUTH"
    )


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

    Cookie settings are hot-reloaded from the managed settings store; email
    bodies are never treated as cookie commands.
    """

    def __init__(self, config, *, settings_store=None, restart_timeout_seconds=None, exit_fn=None):
        self.config = config
        self.downloader = DouyinDownloader(config.douyin)
        self.bilibili_downloader = BilibiliDownloader(config.bilibili)
        self._downloader_registry = DownloaderRegistry()
        self._downloader_registry.register(
            "douyin",
            self.downloader,
            matcher=lambda url: detect_platform(url) == "douyin",
        )
        self._downloader_registry.register(
            "bilibili",
            self.bilibili_downloader,
            matcher=lambda url: detect_platform(url) == "bilibili",
        )
        self._download_executor = DownloadExecutor(self._downloader_registry)
        self._task_service: DownloadTaskService | None = None
        self.extractor = UrlExtractor()
        self._cooldowns: dict[str, float] = {}
        self._project_dir = Path(__file__).parent
        # Settings are shared with the browser process.  Keep this separate
        # from MailStateStore: the latter must remain open while workers drain.
        self._settings = settings_store or SettingsStore(
            default_database_path(self._project_dir / "config.yaml")
        )
        self._boot_id = uuid.uuid4().hex
        self._settings_revision = self._settings.get_revision()
        self._managed_settings = self._settings.managed_values()
        self._lifecycle_thread: threading.Thread | None = None
        self._lifecycle_stop = threading.Event()
        self._run_thread: threading.Thread | None = None
        self._run_finished = threading.Event()
        self._intake_enabled = threading.Event()
        self._intake_enabled.set()
        self._claim_gate = threading.Lock()
        self._imap_lock = threading.Lock()
        self._imap_connection = None
        self._active_lock = threading.Lock()
        self._active_tasks = 0
        self._active_outbox = 0
        self._restart_id: str | None = None
        self._restart_started_at: float | None = None
        self._restart_timeout = float(
            restart_timeout_seconds
            if restart_timeout_seconds is not None
            else os.getenv("BOT_RESTART_DRAIN_TIMEOUT", "300")
        )
        self._exit_fn = exit_fn or os._exit
        self._seen_ids: set[str] = set()  # dedup across poll cycles
        self._pending_retries: dict[str, dict] = {}
        self._pending_retry_lock = threading.RLock()
        self._failure_file_lock = threading.Lock()
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
            self._task_service = DownloadTaskService(
                TaskStore(self._state),
                self._download_executor,
                worker_count=getattr(config.bot, "worker_count", 2),
                platform_worker_counts={
                    "douyin": getattr(config.bot, "douyin_worker_count", 1),
                    "bilibili": getattr(config.bot, "bilibili_worker_count", 1),
                },
                lease_seconds=getattr(config.bot, "lease_seconds", 300),
                heartbeat_seconds=getattr(config.bot, "heartbeat_seconds", 30),
                max_attempts=getattr(config.bot, "transient_retry_attempts", 3),
                retry_delay_seconds=getattr(
                    config.bot, "transient_retry_delay_seconds", 120
                ),
                before_execute=self._before_task_service_execute,
                after_execute=self._after_task_service_execute,
                on_execute_finished=self._after_task_service_finished,
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
        self._held_sender_locks: dict[int, threading.Lock] = {}
        self._backup_cleanup = BackupCleanupScheduler(
            Path(config.douyin.download_path),
            retention_days=config.media_cleanup.backup_retention_days,
            check_interval_days=config.media_cleanup.check_interval_days,
        )
        self._load_pending_retries()
        if self._state is not None:
            self._migrate_legacy_retries()
            self._replay_terminal_failure_projections()
            self._retry_legacy_cleanup()

        # Runtime settings are watched in SQLite.  Do not reload .env here:
        # it is a lower-priority startup source and must never override the
        # managed settings registry in a running process.
        self._env_watch = False

    @staticmethod
    def _assert_legacy_rollback_safe(
        state_db_path: Path, pending_retry_path: Path | None = None
    ) -> None:
        # An unreadable retry mirror cannot be treated as empty: doing so could
        # abandon work that the legacy path would otherwise process. Validate
        # it before inspecting terminal durable records.
        pending: object | None = None
        if pending_retry_path is not None and pending_retry_path.exists():
            try:
                pending = json.loads(pending_retry_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                logger.error(
                    "Cannot validate legacy retry file %s during rollback: %s",
                    pending_retry_path,
                    exc,
                )
                raise RuntimeError(
                    f"cannot disable durable mail: legacy retry file "
                    f"{pending_retry_path} is unreadable or malformed"
                ) from exc
            if not isinstance(pending, dict):
                logger.error(
                    "Legacy retry file %s must contain an object, got %s",
                    pending_retry_path,
                    type(pending).__name__,
                )
                raise RuntimeError(
                    f"cannot disable durable mail: legacy retry file "
                    f"{pending_retry_path} is malformed (expected an object)"
                )
            for key, item in pending.items():
                if not isinstance(item, dict) or not str(item.get("url") or "").strip():
                    logger.error(
                        "Legacy retry file %s contains malformed entry %r",
                        pending_retry_path,
                        key,
                    )
                    raise RuntimeError(
                        f"cannot disable durable mail: legacy retry file "
                        f"{pending_retry_path} contains a malformed entry"
                    )

        with MailStateStore(state_db_path) as rollback_state:
            unfinished = rollback_state.unfinished_work_counts()
            unfinished["events"] = rollback_state.unfinished_event_count("email")
            terminal_legacy_keys = rollback_state.terminal_legacy_retry_keys(strict=True)
        if any(unfinished.values()):
            raise RuntimeError(
                "cannot disable durable mail while SQLite work remains: "
                f"{unfinished}; drain intake/Seen/tasks/outbox before rollback"
            )
        if terminal_legacy_keys and isinstance(pending, dict):
            if terminal_legacy_keys.intersection(
                str(key) for key in pending
            ):
                raise RuntimeError(
                    "cannot disable durable mail while terminal tasks remain in "
                    "the legacy retry file"
                )

    def _replay_terminal_failure_projections(self) -> None:
        """Replay terminal event projections before accepting new mail.

        The event remains the durable source of truth. Replaying all known
        terminal events repairs a crash between task completion and the
        failure-file projection, including events consumed by an older bot
        version. ``task_id`` makes the append idempotent.
        """
        if self._state is None:
            return
        after_id = None
        while True:
            events = self._state.list_task_events(
                limit=500,
                include_consumed=True,
                after_id=after_id,
            )
            if not events:
                return
            for event in events:
                after_id = int(event["id"])
                if str(event.get("event_type") or "") not in {
                    "task.succeeded",
                    "task.partially_succeeded",
                    "task.failed",
                }:
                    continue
                task = self._state.get_task_by_id(int(event["task_id"]))
                consumed = self._state.task_event_consumed(int(event["id"]), "email")
                if task is None or not self._project_terminal_failure(
                    task, event, allow_legacy_unkeyed=consumed
                ):
                    logger.warning(
                        "Could not replay terminal failure projection for event %s",
                        event.get("id"),
                    )

    def run(self) -> None:
        self._run_thread = threading.current_thread()
        self._run_finished.clear()
        cfg = self.config.email
        bot_cfg = self.config.bot

        logger.info(
            "EmailBot starting — mailbox: %s, poll interval: %ds",
            cfg.email,
            cfg.poll_interval,
        )

        self._backup_cleanup.run_if_due()
        self._mark_startup()
        self._start_runtime()
        self._start_lifecycle_watcher()
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
            self._run_finished.set()

    def shutdown(self) -> None:
        """Stop intake and let bounded runtime threads finish their current work."""
        service = getattr(self, "_task_service", None)
        if service is not None:
            service.quiesce()
        self._stop_event.set()
        restart_pending = getattr(self, "_restart_id", None) is not None
        current = threading.current_thread()
        for thread in self._runtime_threads:
            if thread is not current:
                thread.join(timeout=30)
        self._runtime_threads.clear()
        if service is not None:
            service.shutdown(timeout=30)
        lifecycle_stop = getattr(self, "_lifecycle_stop", None)
        if self._state is not None:
            # Never close SQLite underneath an active worker.  Normal restart
            # drains before this point; a manual stop leaves the process to
            # close the connection during interpreter teardown if necessary.
            active = self._active_count()
            if active == 0:
                self._state.close()
            else:
                logger.error("Leaving mail state open because %d worker(s) remain active", active)
        else:
            active = 0
        # During a restart, the lifecycle watcher is the sole writer of
        # restart status and remains alive until it observes the run thread
        # exit (or enforces the drain deadline). A manual shutdown can stop it
        # immediately.
        if lifecycle_stop is not None and not restart_pending:
            lifecycle_stop.set()
        if self._lifecycle_thread is not None and self._lifecycle_thread is not current:
            if not restart_pending:
                self._lifecycle_thread.join(timeout=5)
                self._lifecycle_thread = None

    def _start_runtime(self) -> None:
        if self._state is None or self._runtime_threads:
            return
        service = getattr(self, "_task_service", None)
        if service is not None:
            service.start()
            events = threading.Thread(
                target=self._task_event_worker_loop,
                name="mail-task-events",
                daemon=True,
            )
            events.start()
            self._runtime_threads.append(events)
            # The DownloadTaskService owns download workers.  SMTP and event
            # projection remain entry-specific and stay with EmailBot.
            if self._notifications_enabled():
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
        if self._notifications_enabled():
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

    def _start_lifecycle_watcher(self) -> None:
        if self._lifecycle_thread is not None:
            return
        lifecycle_stop = getattr(self, "_lifecycle_stop", None)
        if lifecycle_stop is not None:
            lifecycle_stop.clear()
        self._lifecycle_thread = threading.Thread(
            target=self._lifecycle_loop, name="bot-settings-watcher", daemon=False
        )
        self._lifecycle_thread.start()

    def _mark_startup(self) -> None:
        """Record this boot and complete a request interrupted by a restart."""
        revision = self._settings.get_revision()
        try:
            self._settings.heartbeat(
                "bot", boot_id=self._boot_id, revision=revision, status="ok"
            )
            # A previous process marks its request restarting immediately
            # before exit.  This UPDATE is idempotent and intentionally done
            # through the shared DB so no stale request remains visible.
            with sqlite3.connect(self._settings.path, timeout=10) as db:
                db.execute(
                    "UPDATE restart_requests SET status='applied', updated_at=?, error=NULL "
                    "WHERE status IN ('restarting', 'forcing') AND revision <= ?",
                    (time.time(), revision),
                )
        except Exception:
            logger.exception("Could not record bot startup lifecycle state")
        self._settings_revision = revision
        self._managed_settings = self._settings.managed_values()

    def _lifecycle_loop(self) -> None:
        """Watch managed settings and coordinate a safe Docker restart."""
        stop = getattr(self, "_lifecycle_stop", self._stop_event)
        while not stop.wait(1.0):
            try:
                revision = self._settings.get_revision()
                self._settings.heartbeat(
                    "bot",
                    boot_id=self._boot_id,
                    revision=revision,
                    status="draining" if self._restart_id else "ok",
                    detail=self._lifecycle_detail(),
                )
                if self._restart_id:
                    self._finish_restart_if_ready()
                    continue
                if revision != self._settings_revision:
                    self._handle_settings_revision(revision)
                request = self._claim_restart_request(revision)
                if request:
                    self._begin_restart(str(request["request_id"]), int(request["revision"]))
            except Exception:
                logger.exception("Settings lifecycle watcher failed")

    def _lifecycle_detail(self) -> str:
        with self._active_lock:
            return f"active_tasks={self._active_tasks},active_outbox={self._active_outbox}"

    def _intake_is_enabled(self) -> bool:
        """Return intake state; tolerate lightweight test doubles/old callers."""
        gate = getattr(self, "_intake_enabled", None)
        return gate is None or gate.is_set()

    def _notifications_enabled(self) -> bool:
        """Return whether SMTP replies are enabled for this process."""
        email_config = getattr(getattr(self, "config", None), "email", None)
        return bool(getattr(email_config, "send_replies", True))

    def _handle_settings_revision(self, revision: int) -> None:
        current = self._settings.managed_values()
        changed = set(current) | set(self._managed_settings)
        changed = {key for key in changed if current.get(key) != self._managed_settings.get(key)}
        self._managed_settings = current
        self._settings_revision = revision
        if changed and changed <= {"douyin.cookie"}:
            # Resolve the complete effective config so reset (which removes
            # the managed override) follows the same legacy env/YAML/default
            # precedence as startup.  A managed clear remains an explicit
            # empty cookie rather than falling through to an older source.
            from config_loader import load_config

            effective = load_config(self._project_dir / "config.yaml")
            self._hot_reload_cookie(effective.douyin.cookie)
            return
        if changed:
            logger.info("Managed settings changed (%s); waiting for restart request", ", ".join(sorted(changed)))

    def _hot_reload_cookie(self, value) -> None:
        cookie = "" if value is None else str(value)
        with self._cookie_lock:
            self.downloader.config.cookie = cookie
        logger.info("Hot-reloaded DOUYIN_COOKIE (%d chars)", len(cookie))

    def _claim_restart_request(self, revision: int) -> dict | None:
        """Atomically claim one queued request, avoiding duplicate drains."""
        claim = getattr(self._settings, "claim_queued_restart", None)
        if callable(claim):
            request = claim()
            if not request:
                return None
            if isinstance(request, str):
                return {"request_id": request, "revision": revision}
            return dict(request)
        # Compatibility fallback for the initial settings-store schema. The
        # production store exposes claim_queued_restart(), but keeping this
        # small fallback makes older checkouts fail safe during an upgrade.
        with sqlite3.connect(self._settings.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM restart_requests WHERE status='queued' "
                "ORDER BY created_at, request_id LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = time.time()
            changed = db.execute(
                "UPDATE restart_requests SET status='draining',updated_at=? "
                "WHERE request_id=? AND status='queued'",
                (now, row["request_id"]),
            ).rowcount
            db.commit()
            return dict(row) if changed else None

    def _begin_restart(self, request_id: str, revision: int) -> None:
        self._restart_id = request_id
        self._restart_started_at = time.monotonic()
        # Serialize the gate with worker claims. Once cleared, no worker can
        # claim another task/outbox item after this point.
        with self._claim_gate:
            self._intake_enabled.clear()
        # Persist the draining transition before stopping the polling loop.
        # This makes an orderly shutdown distinguishable from a process that
        # disappeared before it began draining.
        self._update_restart(request_id, "draining", active_count=self._active_count())
        # Stop the polling loop after its current IMAP operation and close the
        # socket immediately if it is blocked in a read.  The lifecycle
        # watcher has its own stop event and continues enforcing the deadline.
        self._stop_event.set()
        self._close_active_imap()
        logger.info("Draining mail workers for settings restart %s (revision %d)", request_id, revision)

    def _finish_restart_if_ready(self) -> None:
        active = self._active_count()
        started = self._restart_started_at or time.monotonic()
        elapsed = time.monotonic() - started
        run_thread = getattr(self, "_run_thread", None)
        run_finished_event = getattr(self, "_run_finished", None)
        if run_finished_event is not None:
            run_exited = run_finished_event.is_set()
        else:
            # Lightweight callers from before lifecycle tracking have no
            # completion event; a missing run thread means there is nothing
            # left to wait for.
            run_exited = run_thread is None or not run_thread.is_alive()
        run_alive = not run_exited
        if (active or run_alive) and elapsed < self._restart_timeout:
            return
        if active or run_alive:
            self._update_restart(
                self._restart_id,
                "forcing",
                "drain timeout",
                active_count=active,
                detail=(self._lifecycle_detail() + ",run_thread=alive") if run_alive else self._lifecycle_detail(),
            )
            logger.error(
                "Restart drain timed out with %d active worker(s), run_thread_alive=%s; forcing exit",
                active,
                run_alive,
            )
            self._exit_fn(75)
            return
        # The lifecycle watcher is the sole owner of finalization. Closing is
        # idempotent in MailStateStore, so this is safe even if shutdown
        # already closed an idle state connection after its worker joins
        # timed out.
        state = getattr(self, "_state", None)
        if state is not None:
            state.close()
        self._update_restart(
            self._restart_id, "restarting", active_count=0, detail="drained"
        )
        self._settings.heartbeat("bot", boot_id=self._boot_id, status="restarting", detail="drained")
        self._stop_event.set()
        lifecycle_stop = getattr(self, "_lifecycle_stop", None)
        if lifecycle_stop is not None:
            lifecycle_stop.set()

    def _active_count(self) -> int:
        with self._active_lock:
            active = self._active_tasks + self._active_outbox
        service = getattr(self, "_task_service", None)
        if service is not None:
            active += service.active_count()
        return active

    def _update_restart(self, request_id: str, status: str, error=None, **details) -> None:
        """Call the richer lifecycle API while tolerating an old store."""
        try:
            self._settings.update_restart(request_id, status, error, **details)
        except TypeError:
            self._settings.update_restart(request_id, status, error)

    def _maintenance_loop(self) -> None:
        """Recover leases and run retries/cleanup independently of IMAP intake."""
        interval = max(1, min(30, getattr(self.config.email, "poll_interval", 30)))
        while not self._stop_event.wait(interval):
            try:
                if self._state is not None:
                    recovered = self._state.recover_expired()
                    if recovered["tasks"] or recovered["outbox"]:
                        logger.warning("Recovered expired mail leases: %s", recovered)
                    self._replay_terminal_failure_projections()
                    self._retry_legacy_cleanup()
                self._backup_cleanup.run_if_due()
                if self._state is None:
                    self._process_pending_retries(self.config.email, self.config.bot)
            except Exception:
                logger.exception("Independent mail maintenance failed")

    def _task_event_worker_loop(self) -> None:
        """Project durable task events into the mail-specific SMTP outbox."""
        service = getattr(self, "_task_service", None)
        if service is None or self._state is None:
            return
        consumer = "email"
        while not self._stop_event.is_set():
            try:
                events = service.store.events(consumer=consumer, limit=25)
                if not events:
                    self._stop_event.wait(0.25)
                    continue
                for event in events:
                    task_id = int(event["task_id"])
                    task = self._state.get_task_by_id(task_id)
                    if task is None:
                        service.store.consume_event(int(event["id"]), consumer)
                        continue
                    event_type = str(event.get("event_type") or "")
                    if event_type not in {
                        "task.succeeded",
                        "task.partially_succeeded",
                        "task.failed",
                    }:
                        service.store.consume_event(int(event["id"]), consumer)
                        continue
                    if not self._project_terminal_failure(task, event):
                        logger.warning("Could not project failure record for task %d", task_id)
                        continue
                    if not self._notifications_enabled():
                        # Suppression acknowledges the terminal event without
                        # creating an SMTP outbox item. Existing outbox rows
                        # remain untouched and can be delivered after a
                        # restart with notifications enabled.
                        service.store.consume_event(int(event["id"]), consumer)
                        continue
                    if self._state.task_has_outbox(task_id):
                        # Compatibility calls that still atomically created an
                        # outbox item must not receive a second notification.
                        service.store.consume_event(int(event["id"]), consumer)
                        continue
                    notification = self._task_notification(task, event)
                    if notification is None:
                        service.store.consume_event(int(event["id"]), consumer)
                        continue
                    event_payload = event.get("payload") or {}
                    if not isinstance(event_payload, dict):
                        event_payload = {}
                    result = event_payload.get("result") or task.get("result") or {}
                    if not isinstance(result, dict):
                        result = {}
                    outbox_event = "partial-failed" if result.get("partial") else (
                        "failed" if event_type == "task.failed" else "completed"
                    )
                    service.store.project_event(
                        int(event["id"]),
                        consumer,
                        outbox_event=outbox_event,
                        outbox_payload=notification,
                    )
            except Exception:
                logger.exception("Durable task event projector failed")
                self._stop_event.wait(1)

    @staticmethod
    def _task_notification(task: dict, event: dict) -> dict | None:
        payload = task.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        sender = str(payload.get("sender") or "")
        if not sender:
            return None
        event_payload = event.get("payload") or {}
        if not isinstance(event_payload, dict):
            event_payload = {}
        result = event_payload.get("result") or task.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        successful = event.get("event_type") in {
            "task.succeeded",
            "task.partially_succeeded",
        }
        if successful or result.get("success"):
            filepath = result.get("filepath") or "未知路径"
            if result.get("partial"):
                body = _format_success_reply(
                    result, filepath, prefix="部分下载完成。"
                )
            else:
                body = _format_success_reply(result, filepath)
            subject = _success_subject_status(result)
        else:
            error = event_payload.get("error") or task.get("last_error") or "未知错误"
            body = f"下载失败：{error}{_download_failure_hint()}"
            subject = "下载失败"
        return {"to_addr": sender, "body": body, "subject_status": subject}

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
                with self._claim_gate:
                    if not self._intake_is_enabled():
                        return
                    tasks = self._state.claim_tasks(
                        1,
                        lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                        worker_id=worker_id,
                    )
                    # Count a claimed lease while still holding the claim
                    # gate.  Restart drain clears this gate, so it must not
                    # observe a claimed task as idle in the hand-off window
                    # before the worker starts processing it.
                    if tasks:
                        with self._active_lock:
                            self._active_tasks += len(tasks)
                if not tasks:
                    self._stop_event.wait(0.5)
                    continue
                for task in tasks:
                    try:
                        self._process_durable_task(task)
                    finally:
                        with self._active_lock:
                            self._active_tasks -= 1
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
                        notification=notification if self._notifications_enabled() else None,
                        outbox_event=(task.get("payload") or {}).get("event", "notice"),
                    )
                else:
                    self._complete_durable_failure(task, token, "notice payload missing")
                return
            result = self._download_url(task["original_url"])
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
                    notification=payload if self._notifications_enabled() else None,
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

    def _before_task_service_execute(self, task) -> None:
        """Hold a sender lock across cooldown check and the full download."""
        state = getattr(self, "_state", None)
        if state is None:
            return
        row = state.get_task_by_id(task.task_id)
        sender = str(((row or {}).get("payload") or {}).get("sender") or "")
        if not sender:
            return
        with self._sender_locks_guard:
            lock = self._sender_locks.setdefault(sender, threading.Lock())
        lock.acquire()
        try:
            cooldown = max(0, getattr(self.config.bot, "cooldown_seconds", 0))
            remaining = cooldown - (time.time() - self._cooldowns.get(sender, 0))
            if remaining > 0:
                getattr(self, "_stop_event", threading.Event()).wait(remaining)
            with self._sender_locks_guard:
                held = getattr(self, "_held_sender_locks", None)
                if held is None:
                    held = self._held_sender_locks = {}
                held[task.task_id] = lock
        except Exception:
            lock.release()
            raise

    def _after_task_service_execute(self, task, result) -> None:
        if result.success:
            state = getattr(self, "_state", None)
            row = state.get_task_by_id(task.task_id) if state is not None else None
            sender = str(((row or {}).get("payload") or {}).get("sender") or "")
            if sender:
                self._cooldowns[sender] = time.time()

    def _after_task_service_finished(self, task) -> None:
        """Release sender serialization even if the executor raises."""
        with self._sender_locks_guard:
            held = getattr(self, "_held_sender_locks", None)
            lock = held.pop(task.task_id, None) if held is not None else None
        if lock is not None:
            lock.release()

    def _project_terminal_failure(
        self,
        task: dict,
        event: dict,
        *,
        allow_legacy_unkeyed: bool = False,
    ) -> bool:
        """Persist a terminal failure record before acknowledging its event."""
        event_type = str(event.get("event_type") or "")
        if event_type not in {"task.succeeded", "task.partially_succeeded", "task.failed"}:
            return True
        payload = task.get("payload") or {}
        event_payload = event.get("payload") or {}
        if not isinstance(event_payload, dict):
            event_payload = {}
        result = event_payload.get("result") or task.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        if event_type == "task.failed" or result.get("partial"):
            if not payload.get("sender"):
                return True
            error = (
                event_payload.get("error")
                or task.get("last_error")
                or result.get("error")
                or "；".join(str(item) for item in result.get("failed_items") or [])
                or "未知错误"
            )
            if not self._record_failed_link(
                {
                    "sender": payload.get("sender", ""),
                    "platform": task.get("platform", ""),
                    "attempts": task.get("attempts", ""),
                    "url": task.get("original_url", ""),
                },
                error,
                task_id=int(task["id"]),
                allow_legacy_unkeyed=allow_legacy_unkeyed,
            ):
                return False
        # A terminal durable event is the safe point to remove the mirrored
        # legacy retry. If removal fails, maintenance retains the retry mirror.
        self._remove_legacy_retry(payload)
        return True

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
            ) + f"\n最后错误：{error}" + _download_failure_hint()
            subject_status = "部分下载失败"
        else:
            body = f"下载失败：{error}{_download_failure_hint()}"
            subject_status = "下载失败"
        failed = self._state.fail_task(
            int(task["id"]),
            token,
            error,
            notification=(
                {
                    "to_addr": sender,
                    "body": body,
                    "subject_status": subject_status,
                }
                if self._notifications_enabled()
                else None
            ),
            outbox_event="partial-failed" if result and result.get("success") else "failed",
        )
        if failed is None:
            return
        recorded = self._record_failed_link(
            {
                "sender": sender,
                "platform": task.get("platform", ""),
                "attempts": task.get("attempts", ""),
                "url": task.get("original_url", ""),
            },
            error,
            task_id=int(task["id"]),
        )
        if recorded:
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

    def _outbox_worker_loop(self) -> None:
        assert self._state is not None
        if not self._notifications_enabled():
            logger.info("SMTP outbox worker disabled by EMAIL_SEND_REPLIES")
            return
        while not self._stop_event.is_set():
            try:
                with self._claim_gate:
                    if not self._intake_is_enabled():
                        return
                    items = self._state.claim_outbox(
                        1,
                        lease_seconds=getattr(self.config.bot, "lease_seconds", 300),
                        worker_id="smtp-outbox",
                    )
                    # See the task worker above: restart drain must account
                    # for every claimed outbox lease before releasing the
                    # claim gate.
                    if items:
                        with self._active_lock:
                            self._active_outbox += len(items)
                if not items:
                    self._stop_event.wait(0.5)
                    continue
                for item in items:
                    try:
                        self._deliver_outbox(item)
                    finally:
                        with self._active_lock:
                            self._active_outbox -= 1
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
        """Compatibility hook; SQLite lifecycle watcher owns runtime reloads."""
        return

    def _poll_once(self, cfg, bot_cfg) -> None:
        if not self._intake_is_enabled():
            return
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
                if not self._intake_is_enabled():
                    break
                self._process_email(mail, msg_id, cfg, bot_cfg)
        finally:
            self._clear_active_imap(mail)
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
            baseline_uid = None
            if reset:
                # A reset must establish a baseline without routing historical
                # Seen mail.  Keep it provisional until every candidate has
                # been fetched and durably routed below.
                status, data = mail.uid("search", None, "ALL")
                if status != "OK":
                    logger.warning("IMAP UID SEARCH failed for ALL during reset")
                    return
                baseline_uid = self._max_uid_search_result(data)
            else:
                start_uid = int(position["last_uid"]) + 1

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
            # service's normal UNSEEN poll contract. During a reset, ALL is
            # only the baseline above; UNSEEN is the sole search that adds
            # candidates, so historical Seen mail is never fetched.
            criteria_list = ["UNSEEN"] if reset else [f"UID {start_uid}:*", "UNSEEN"]
            for criteria in criteria_list:
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
                    if not reset and criteria.startswith("UID ") and parsed_uid < start_uid:
                        continue
                    uids.add(parsed_uid)
            uids = sorted(uids)
            reconciliation_ok = True
            for uid in uids:
                if not self._intake_is_enabled():
                    reconciliation_ok = False
                    break
                fetched = self._imap_fetch_uid(mail, uid)
                if fetched is None:
                    # Do not advance beyond an un-fetchable UID. The next
                    # reconciliation will retry it instead of silently
                    # skipping a message.
                    logger.warning("Could not fetch IMAP UID %d; stopping reconciliation", uid)
                    reconciliation_ok = False
                    break
                source_id = f"{mailbox}:{uidvalidity}:{uid}"
                try:
                    accepted, routing_failed = self._durably_accept_email(
                        mail,
                        mailbox,
                        uidvalidity,
                        uid,
                        source_id,
                        fetched,
                        cfg,
                        bot_cfg,
                        advance_position=not reset,
                    )
                    if routing_failed:
                        # Parsing/routing failures are quarantined by the
                        # intake helper, but still invalidate a provisional
                        # reset baseline.
                        reconciliation_ok = False
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
                        advance_position=not reset,
                    )
                    # Quarantine isolates one malformed message, but a reset
                    # must not claim its baseline after any routing exception.
                    reconciliation_ok = False
                if not accepted:
                    reconciliation_ok = False
                    break
            if reset and reconciliation_ok:
                assert baseline_uid is not None
                self._state.set_mailbox_position(mailbox, uidvalidity, baseline_uid)
        finally:
            self._clear_active_imap(mail)
            self._safe_logout(mail)

    def _register_active_imap(self, mail) -> None:
        lock = getattr(self, "_imap_lock", None)
        if lock is None:
            self._imap_connection = mail
            return
        with lock:
            self._imap_connection = mail

    def _clear_active_imap(self, mail) -> None:
        lock = getattr(self, "_imap_lock", None)
        if lock is None:
            if getattr(self, "_imap_connection", None) is mail:
                self._imap_connection = None
            return
        with lock:
            if self._imap_connection is mail:
                self._imap_connection = None

    def _close_active_imap(self) -> None:
        """Interrupt a potentially blocked IMAP operation during restart."""
        lock = getattr(self, "_imap_lock", None)
        if lock is None:
            mail = getattr(self, "_imap_connection", None)
            self._imap_connection = None
        else:
            with lock:
                mail = self._imap_connection
                self._imap_connection = None
        if mail is not None:
            self._safe_logout(mail)

    @staticmethod
    def _max_uid_search_result(data) -> int:
        """Return the largest valid UID from an IMAP UID SEARCH response."""
        if not data:
            return 0
        value = data[0] if isinstance(data, (list, tuple)) else data
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        if not isinstance(value, str):
            return 0
        uids = [int(item) for item in value.split() if item.isdigit()]
        return max(uids, default=0)

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
        *,
        advance_position: bool = True,
    ) -> tuple[bool, bool]:
        """Commit source identity and route work before attempting IMAP STORE."""
        assert self._state is not None
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        urls: list[str] = []
        platform = None
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
                if (
                    not getattr(bot_cfg, "subject_keyword", "")
                    or getattr(bot_cfg, "subject_keyword", "") in subject
                ):
                    url = self.extractor.extract(subject + " " + body)
                    if url:
                        urls = [url]
                        platform = detect_platform(url)
                else:
                    logger.debug(
                        "Skipping durable download email without subject keyword "
                        "from %s: %s",
                        sender,
                        subject,
                    )
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
            task_service = getattr(self, "_task_service", None)
            accept_mail = (
                task_service.accept_mail_message
                if task_service is not None
                else self._state.accept_message
            )
            accepted = accept_mail(
                mailbox,
                uidvalidity,
                uid,
                source_id,
                urls,
                metadata=metadata
                if routing_error
                else {**metadata, "platform": platform},
                platform=platform,
                max_attempts=getattr(bot_cfg, "transient_retry_attempts", 3),
                advance_position=advance_position,
            )
        except Exception:
            logger.exception("Durable intake failed for UID %d", uid)
            return False, bool(routing_error)

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
            # A source stays pending until URL routing (or the safe
            # empty/quarantine intake) has been persisted.
            try:
                if not self._state.mark_intake_complete(source_id):
                    logger.warning("Could not finalize durable intake for UID %d", uid)
                    return False, bool(routing_error)
            except Exception:
                logger.exception("Could not finalize durable intake for UID %d", uid)
                return False, bool(routing_error)

        try:
            if _mark_seen_uid(mail, uid):
                self._state.ack_message(source_id)
        except Exception:
            logger.warning("Durable intake committed but IMAP \\Seen ACK failed for UID %d", uid)
        return True, bool(routing_error)

    def _quarantine_durable_email(
        self,
        mail,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        source_id: str,
        raw: bytes,
        error: BaseException,
        *,
        advance_position: bool = True,
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
            task_service = getattr(self, "_task_service", None)
            accept_mail = (
                task_service.accept_mail_message
                if task_service is not None
                else self._state.accept_message
            )
            accepted = accept_mail(
                mailbox,
                uidvalidity,
                uid,
                source_id,
                [],
                metadata=metadata,
                advance_position=advance_position,
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
        # ── Normal: download ───────────────────────────────────────
        url = self.extractor.extract(subject + " " + body)
        if url is None:
            logger.debug("No supported URL found from %s (subject: %s)", sender, subject)
            _mark_seen(mail, msg_id)
            return

        # Keep keyword-skipped legacy mail unseen for the next poll, while
        # retaining the existing no-URL behavior above.
        subject_keyword = getattr(bot_cfg, "subject_keyword", "") or ""
        if subject_keyword and subject_keyword not in subject:
            logger.debug(
                "Skipping download email without subject keyword from %s: %s",
                sender,
                subject,
            )
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

            # Build helpful hints
            error_msg += _download_failure_hint()
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

    def _record_failed_link(
        self,
        item: dict,
        error_msg: str,
        *,
        task_id: int | None = None,
        allow_legacy_unkeyed: bool = False,
    ) -> bool:
        with self._failure_file_lock:
            try:
                self._failed_links_file.parent.mkdir(parents=True, exist_ok=True)
                if task_id is not None and self._failed_links_file.exists():
                    failure_text = self._failed_links_file.read_text(encoding="utf-8")
                    marker = f"task_id={task_id}\t"
                    if marker in failure_text:
                        return True
                    # Pre-task-id versions wrote unkeyed rows before the
                    # durable terminal commit. An exact match is treated as
                    # the already-projected row during replay, preventing a
                    # one-time upgrade duplicate when the old projection did
                    # succeed. New rows remain keyed by task_id.
                    if allow_legacy_unkeyed and self._has_unkeyed_failure_match(
                        failure_text, item, error_msg
                    ):
                        logger.info(
                            "Reusing legacy failure record for task %d: %s",
                            task_id,
                            item.get("url", ""),
                        )
                        return True
                line = (
                    f"{_now_iso()}\t"
                    f"task_id={task_id if task_id is not None else ''}\t"
                    f"sender={item.get('sender', '')}\t"
                    f"platform={item.get('platform', '')}\t"
                    f"attempts={item.get('attempts', '')}\t"
                    f"url={item.get('url', '')}\t"
                    f"error={error_msg.replace(chr(9), ' ')}\n"
                )
                with self._failed_links_file.open("a", encoding="utf-8") as f:
                    f.write(line)
                logger.info("Recorded failed link: %s", item.get("url", ""))
                return True
            except (OSError, UnicodeError) as exc:
                logger.error("Failed to record failed link in %s: %s", self._failed_links_file, exc)
                return False

    @staticmethod
    def _has_unkeyed_failure_match(text: str, item: dict, error_msg: str) -> bool:
        expected = {
            "sender": str(item.get("sender", "")),
            "platform": str(item.get("platform", "")),
            "url": str(item.get("url", "")),
            "error": str(error_msg).replace(chr(9), " "),
        }
        for line in text.splitlines():
            fields: dict[str, str] = {}
            for field in line.split("\t")[1:]:
                key, separator, value = field.partition("=")
                if separator:
                    fields[key] = value
            if fields.get("task_id") in {None, ""} and all(
                fields.get(key) == value for key, value in expected.items()
            ):
                return True
        return False

    def _download_url(self, url: str) -> dict:
        """Dispatch a supported URL to the correct downloader."""
        executor = getattr(self, "_download_executor", None)
        if executor is not None:
            return executor.execute(url).to_dict()
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

    # ── IMAP / SMTP helpers ───────────────────────────────────────

    def _imap_connect(self, cfg):
        logger.debug("Connecting to IMAP %s:%d", cfg.imap_server, cfg.imap_port)
        # Pass the timeout into the constructor as well as applying it to the
        # resulting socket.  This bounds the connect/TLS handshake and makes
        # the socket available to restart interruption as soon as possible.
        mail = imaplib.IMAP4_SSL(cfg.imap_server, cfg.imap_port, timeout=30)
        self._register_active_imap(mail)
        try:
            # Set a socket timeout so broken connections don't hang the bot.
            # 30s is enough for normal IMAP operations but prevents infinite
            # hangs when the remote side has torn down the connection.
            mail.socket().settimeout(30)
            mail.login(cfg.email, cfg.password)
            return mail
        except Exception:
            self._clear_active_imap(mail)
            self._safe_logout(mail)
            raise

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
        if not getattr(cfg, "send_replies", True):
            logger.info("SMTP reply suppressed for %s", to_addr)
            return
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
