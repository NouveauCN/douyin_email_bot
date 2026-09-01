"""Reusable in-process download execution and durable task orchestration."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from download_types import (
    DownloadResult,
    ErrorCode,
    RetryClass,
    TaskRequest,
    TaskSnapshot,
    TaskStatus,
)
from task_store import TaskStore

logger = logging.getLogger("DownloadTaskService")


class Downloader(Protocol):
    """Minimal platform adapter contract."""

    def download(self, url: str) -> Mapping[str, Any] | DownloadResult: ...


Matcher = Callable[[str], bool]
TaskCallback = Callable[[TaskSnapshot], None]


@dataclass(frozen=True, slots=True)
class _Adapter:
    platform: str
    downloader: Downloader | Callable[[str], Mapping[str, Any] | DownloadResult]
    matcher: Matcher

    def download(self, url: str) -> Mapping[str, Any] | DownloadResult:
        method = getattr(self.downloader, "download", None)
        return method(url) if callable(method) else self.downloader(url)  # type: ignore[misc]


class DownloaderRegistry:
    """Ordered URL router.  Registering a platform never imports its SDK."""

    def __init__(self) -> None:
        self._adapters: dict[str, _Adapter] = {}
        self._lock = threading.RLock()

    def register(
        self,
        platform: str,
        downloader: Downloader | Callable[[str], Mapping[str, Any] | DownloadResult],
        matcher: Matcher | None = None,
        *,
        replace: bool = False,
    ) -> None:
        platform = str(platform).strip().lower()
        if not platform:
            raise ValueError("platform must not be empty")
        if not callable(getattr(downloader, "download", None)) and not callable(downloader):
            raise TypeError("downloader must expose download(url) or be callable")
        if matcher is None:
            candidate = getattr(downloader, "matches", None)
            if callable(candidate):
                matcher = candidate
            else:
                raise TypeError("matcher is required for a downloader without matches(url)")
        if not callable(matcher):
            raise TypeError("matcher must be callable")
        with self._lock:
            if platform in self._adapters and not replace:
                raise ValueError(f"downloader already registered: {platform}")
            self._adapters[platform] = _Adapter(platform, downloader, matcher)

    def unregister(self, platform: str) -> bool:
        with self._lock:
            return self._adapters.pop(str(platform).strip().lower(), None) is not None

    def platforms(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._adapters)

    def resolve(self, url: str) -> str | None:
        with self._lock:
            adapters = tuple(self._adapters.values())
        for adapter in adapters:
            try:
                if adapter.matcher(url):
                    return adapter.platform
            except Exception:
                logger.exception("Downloader URL matcher failed for %s", adapter.platform)
        return None

    def adapter(self, platform: str) -> _Adapter | None:
        with self._lock:
            return self._adapters.get(platform)

    def execute(self, url: str) -> DownloadResult:
        with self._lock:
            adapters = tuple(self._adapters.values())
        for adapter in adapters:
            try:
                matches = adapter.matcher(url)
            except Exception:
                logger.exception("Downloader URL matcher failed for %s", adapter.platform)
                continue
            if not matches:
                continue
            try:
                return DownloadResult.from_mapping(adapter.download(url))
            except Exception as exc:
                logger.exception("Downloader %s failed for %s", adapter.platform, url)
                return DownloadResult(
                    success=False,
                    error=str(exc) or "download adapter failed",
                    error_code=ErrorCode.UNKNOWN,
                    retry_class=RetryClass.TRANSIENT,
                    retryable=True,
                )
        return DownloadResult(
            success=False,
            error="unsupported URL",
            error_code=ErrorCode.UNSUPPORTED_URL,
            retry_class=RetryClass.PERMANENT,
            retryable=False,
        )


def _classify_result(result: DownloadResult) -> DownloadResult:
    """Fill stable retry metadata for older platform adapters."""
    if result.success:
        return result
    if result.error_code != ErrorCode.UNKNOWN or result.retry_class != RetryClass.NONE:
        return result
    text = (result.error or "").lower()
    if any(part in text for part in ("timeout", "timed out", "超时")):
        code, retry = ErrorCode.TIMEOUT, RetryClass.TRANSIENT
    elif any(part in text for part in ("network", "connection", "网络", "连接")):
        code, retry = ErrorCode.NETWORK, RetryClass.TRANSIENT
    elif any(part in text for part in ("cookie", "登录", "私密", "删除")):
        code, retry = ErrorCode.COOKIE_REQUIRED, RetryClass.TRANSIENT
    else:
        code, retry = ErrorCode.DOWNLOAD_FAILED, RetryClass.PERMANENT
    return DownloadResult(
        **{
            **result.to_dict(),
            "error_code": code,
            "retry_class": retry,
            "retryable": retry == RetryClass.TRANSIENT,
        }
    )


class DownloadExecutor:
    """Stateless registry-backed executor shared by durable and legacy paths."""

    def __init__(self, registry: DownloaderRegistry) -> None:
        self.registry = registry

    def platform_for(self, url: str) -> str | None:
        return self.registry.resolve(url)

    def execute(self, url: str) -> DownloadResult:
        return _classify_result(self.registry.execute(url))


class DownloadTaskService:
    """Durable task coordinator with bounded workers and expiring leases."""

    def __init__(
        self,
        store: TaskStore,
        executor: DownloadExecutor,
        *,
        worker_count: int = 1,
        platform_worker_counts: Mapping[str, int] | None = None,
        lease_seconds: float = 300,
        heartbeat_seconds: float = 30,
        max_attempts: int = 3,
        retry_delay_seconds: float = 120,
        clock: Callable[[], float] = time.time,
        before_execute: Callable[[TaskSnapshot], None] | None = None,
        after_execute: Callable[[TaskSnapshot, DownloadResult], None] | None = None,
        on_execute_finished: TaskCallback | None = None,
    ) -> None:
        if worker_count < 1 or max_attempts < 1:
            raise ValueError("worker_count and max_attempts must be positive")
        if lease_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("lease and heartbeat must be positive")
        self.store = store
        self.executor = executor
        self.worker_count = int(worker_count)
        self.platform_worker_counts = {
            str(key): max(1, int(value)) for key, value in (platform_worker_counts or {}).items()
        }
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = min(float(heartbeat_seconds), self.lease_seconds / 3)
        self.max_attempts = int(max_attempts)
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.clock = clock
        self.before_execute = before_execute
        self.after_execute = after_execute
        self.on_execute_finished = on_execute_finished
        self._stop = threading.Event()
        self._quiesced = threading.Event()
        self._claim_gate = threading.Lock()
        self._thread_lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._callbacks: dict[int, TaskCallback] = {}
        self._callback_lock = threading.RLock()
        self._platform_slots = {
            key: threading.BoundedSemaphore(value)
            for key, value in self.platform_worker_counts.items()
        }

    @property
    def running(self) -> bool:
        with self._thread_lock:
            return bool(self._threads)

    def active_count(self) -> int:
        with self._thread_lock:
            return self._active

    def submit(
        self,
        request: TaskRequest,
        on_update: TaskCallback | None = None,
    ) -> TaskSnapshot:
        platform = self.executor.platform_for(request.url)
        snapshot = self.store.submit(
            request,
            platform=platform,
            max_attempts=self.max_attempts,
            now=self.clock(),
        )
        if on_update is not None:
            with self._callback_lock:
                self._callbacks[snapshot.task_id] = on_update
        return snapshot

    def get(self, task_id: int) -> TaskSnapshot | None:
        return self.store.get(task_id)

    def accept_mail_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Preserve atomic IMAP source/task acceptance for the mail adapter."""
        return self.store.accept_mail_message(*args, **kwargs)

    def accept_qq_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Atomically persist one QQ source, task, and confirmation outbox."""
        # TaskStore deliberately keeps its public facade source-agnostic; QQ
        # intake is nevertheless part of the same SQLite transaction.
        return self.store.state.accept_qq_message(*args, **kwargs)

    def start(self) -> None:
        with self._thread_lock:
            if self._threads:
                return
            self._stop.clear()
            self._quiesced.clear()
            self._threads = []
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(f"download-service-{index + 1}",),
                    name=f"download-service-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            maintenance = threading.Thread(
                target=self._maintenance_loop,
                name="download-service-maintenance",
                daemon=True,
            )
            maintenance.start()
            self._threads.append(maintenance)

    def quiesce(self) -> None:
        """Stop accepting new claims while allowing current downloads to finish."""
        self._quiesced.set()

    def drain(self, timeout: float = 300) -> bool:
        self.quiesce()
        deadline = self.clock() + max(0.0, float(timeout))
        while self.clock() < deadline:
            with self._thread_lock:
                active = self._active
            if active == 0:
                return True
            self._stop.wait(min(0.25, max(0.01, deadline - self.clock())))
        return False

    def shutdown(self, *, timeout: float = 30) -> None:
        self.quiesce()
        self._stop.set()
        current = threading.current_thread()
        with self._thread_lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not current:
                thread.join(timeout=max(0.0, timeout))
        with self._thread_lock:
            self._threads.clear()

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                with self._claim_gate:
                    if self._quiesced.is_set():
                        return
                    tasks = self.store.claim(
                        1,
                        worker_id=worker_id,
                        lease_seconds=self.lease_seconds,
                        now=self.clock(),
                    )
                    if tasks:
                        with self._thread_lock:
                            self._active += len(tasks)
                if not tasks:
                    self._stop.wait(0.25)
                    continue
                for task in tasks:
                    try:
                        self._process(task)
                    finally:
                        with self._thread_lock:
                            self._active -= 1
            except Exception:
                logger.exception("Download task worker %s failed", worker_id)
                self._stop.wait(1)

    def _process(self, task: TaskSnapshot) -> None:
        token = task.lease_token
        if not token:
            return
        slot = self._platform_slots.get(task.platform or "")
        if slot is not None and not slot.acquire(blocking=False):
            self.store.release(
                task.task_id,
                token,
                next_attempt_at=self.clock() + 1,
                now=self.clock(),
            )
            return
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task.task_id, token, heartbeat_stop),
            name=f"download-service-heartbeat-{task.task_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if self.before_execute is not None:
                self.before_execute(task)
            result = self.executor.execute(task.url)
            if self.after_execute is not None:
                try:
                    self.after_execute(task, result)
                except Exception:
                    logger.exception("Post-download task hook failed for %d", task.task_id)
            attempts = task.attempts
            if result.success:
                if result.partial and result.retryable and attempts < self._max_attempts(task):
                    updated = self.store.fail(
                        task.task_id,
                        token,
                        result.error or "partial download",
                        result=result,
                        retry_at=self.clock() + self.retry_delay_seconds,
                        error_code=result.error_code.value,
                        retry_class=result.retry_class.value,
                        now=self.clock(),
                    )
                else:
                    updated = self.store.complete(
                        task.task_id,
                        token,
                        result,
                        status=(
                            TaskStatus.PARTIALLY_SUCCEEDED.value
                            if result.partial
                            else TaskStatus.SUCCEEDED.value
                        ),
                        now=self.clock(),
                    )
            elif result.retryable and attempts < self._max_attempts(task):
                updated = self.store.fail(
                    task.task_id,
                    token,
                    result.error or "download failed",
                    result=result,
                    retry_at=self.clock() + self.retry_delay_seconds,
                    error_code=result.error_code.value,
                    retry_class=result.retry_class.value,
                    now=self.clock(),
                )
            else:
                updated = self.store.fail(
                    task.task_id,
                    token,
                    result.error or "download failed",
                    result=result,
                    error_code=result.error_code.value,
                    retry_class=result.retry_class.value,
                    now=self.clock(),
                )
            if updated is not None:
                self._notify(updated)
        except Exception as exc:
            logger.exception("Task %d execution failed", task.task_id)
            if task.attempts < self._max_attempts(task):
                updated = self.store.fail(
                    task.task_id,
                    token,
                    str(exc),
                    retry_at=self.clock() + self.retry_delay_seconds,
                    error_code=ErrorCode.UNKNOWN.value,
                    retry_class=RetryClass.TRANSIENT.value,
                    now=self.clock(),
                )
            else:
                updated = self.store.fail(
                    task.task_id,
                    token,
                    str(exc),
                    error_code=ErrorCode.UNKNOWN.value,
                    retry_class=RetryClass.PERMANENT.value,
                    now=self.clock(),
                )
            if updated is not None:
                self._notify(updated)
        finally:
            if self.on_execute_finished is not None:
                try:
                    self.on_execute_finished(task)
                except Exception:
                    logger.exception("Task cleanup callback failed for task %d", task.task_id)
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            if slot is not None:
                slot.release()

    def _max_attempts(self, task: TaskSnapshot) -> int:
        return task.max_attempts or self.max_attempts

    def _notify(self, snapshot: TaskSnapshot) -> None:
        with self._callback_lock:
            callback = self._callbacks.get(snapshot.task_id)
            if snapshot.status in (
                TaskStatus.SUCCEEDED,
                TaskStatus.PARTIALLY_SUCCEEDED,
                TaskStatus.FAILED,
            ):
                self._callbacks.pop(snapshot.task_id, None)
        if callback is None:
            return
        try:
            callback(snapshot)
        except Exception:
            logger.exception("Task update callback failed for task %d", snapshot.task_id)

    def _heartbeat_loop(self, task_id: int, token: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                if self.store.heartbeat(
                    task_id,
                    token,
                    lease_seconds=self.lease_seconds,
                    now=self.clock(),
                ) is None:
                    return
            except Exception:
                logger.exception("Task heartbeat failed for %d", task_id)

    def _maintenance_loop(self) -> None:
        interval = max(1.0, min(30.0, self.heartbeat_seconds))
        while not self._stop.wait(interval):
            try:
                self.store.recover_expired(now=self.clock())
            except Exception:
                logger.exception("Download task lease recovery failed")


__all__ = [
    "Downloader",
    "DownloaderRegistry",
    "DownloadExecutor",
    "DownloadTaskService",
]
