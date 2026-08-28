"""Generic task persistence facade backed by the existing mail SQLite DB.

``MailStateStore`` remains the compatibility owner of mailbox and SMTP tables.
This facade gives the download service a source-agnostic API and keeps the
mail-specific schema details out of entry adapters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from download_types import DownloadResult, TaskRequest, TaskSnapshot
from mail_state import MailStateStore


_SECRET_KEY_PARTS = ("cookie", "password", "passwd", "secret", "auth", "token")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                return True
            if _contains_secret_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


class TaskStore:
    """Source-agnostic task store using the shared durable SQLite connection."""

    def __init__(
        self,
        store: MailStateStore | str | Path,
        *,
        default_max_attempts: int | None = None,
    ) -> None:
        if isinstance(store, MailStateStore):
            self.state = store
            self._owns_state = False
        else:
            self.state = MailStateStore(store)
            self._owns_state = True
        if default_max_attempts is not None and default_max_attempts < 1:
            raise ValueError("default_max_attempts must be positive")
        self.default_max_attempts = default_max_attempts

    def close(self) -> None:
        if self._owns_state:
            self.state.close()

    @staticmethod
    def _db_source_id(request: TaskRequest) -> str:
        # The legacy foreign key still points at source_messages.  A namespaced
        # synthetic source keeps that compatibility while the new columns make
        # the namespace explicit for callers and future migrations.
        return request.source.key

    @staticmethod
    def _safe_metadata(request: TaskRequest) -> dict:
        if _contains_secret_key(request.metadata):
            raise ValueError("task metadata must not contain secret-like fields")
        return dict(request.metadata)

    def submit(
        self,
        request: TaskRequest,
        *,
        platform: str | None = None,
        max_attempts: int | None = None,
        now: float | None = None,
    ) -> TaskSnapshot:
        metadata = self._safe_metadata(request)
        attempts = max_attempts if max_attempts is not None else self.default_max_attempts
        if attempts is not None and attempts < 1:
            raise ValueError("max_attempts must be positive")
        row = self.state.enqueue_task(
            self._db_source_id(request),
            request.url,
            payload=metadata,
            platform=platform,
            source_kind=request.source.kind,
            external_source_id=request.source.external_id,
            max_attempts=attempts,
            now=now,
        )
        return TaskSnapshot.from_record(row)

    def accept_mail_message(
        self,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        source_message_id: str,
        urls: list[str] | tuple[str, ...] | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        platform: str | None = None,
        max_attempts: int | None = None,
        advance_position: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically persist mail intake and its task bindings.

        This is intentionally the only mail-shaped method on the generic
        facade.  It preserves the UID position/source transaction while the
        service owns execution after the commit.
        """
        safe_metadata = dict(metadata or {})
        if _contains_secret_key(safe_metadata):
            raise ValueError("mail metadata must not contain secret-like fields")
        return self.state.accept_message(
            mailbox,
            uidvalidity,
            uid,
            source_message_id,
            urls,
            metadata=safe_metadata,
            platform=platform,
            max_attempts=max_attempts,
            advance_position=advance_position,
            now=now,
        )

    def get(self, task_id: int) -> TaskSnapshot | None:
        row = self.state.get_task_by_id(task_id)
        return TaskSnapshot.from_record(row) if row is not None else None

    def claim(
        self,
        limit: int = 1,
        *,
        platform: str | None = None,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> list[TaskSnapshot]:
        return [
            TaskSnapshot.from_record(row)
            for row in self.state.claim_tasks(
                limit,
                platform=platform,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
        ]

    def heartbeat(
        self,
        task_id: int,
        lease_token: str,
        *,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        row = self.state.heartbeat_task(
            task_id, lease_token, lease_seconds=lease_seconds, now=now
        )
        return TaskSnapshot.from_record(row) if row is not None else None

    def release(
        self,
        task_id: int,
        lease_token: str,
        *,
        next_attempt_at: float | None = None,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        row = self.state.release_task(
            task_id, lease_token, next_attempt_at=next_attempt_at, now=now
        )
        return TaskSnapshot.from_record(row) if row is not None else None

    def complete(
        self,
        task_id: int,
        lease_token: str,
        result: DownloadResult | Mapping[str, Any] | None,
        *,
        status: str | None = None,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        normalized = DownloadResult.from_mapping(result or {"success": True})
        final_status = status or (
            "partially_succeeded" if normalized.partial else "succeeded"
        )
        row = self.state.complete_task(
            task_id,
            lease_token,
            result=normalized.to_dict(),
            status=final_status,
            error_code=normalized.error_code.value,
            retry_class=normalized.retry_class.value,
            now=now,
        )
        return TaskSnapshot.from_record(row) if row is not None else None

    def fail(
        self,
        task_id: int,
        lease_token: str,
        error: str,
        *,
        result: DownloadResult | Mapping[str, Any] | None = None,
        retry_at: float | None = None,
        retry: bool = False,
        error_code: str | None = None,
        retry_class: str | None = None,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        result_payload = (
            DownloadResult.from_mapping(result).to_dict() if result is not None else None
        )
        row = self.state.fail_task(
            task_id,
            lease_token,
            error,
            result=result_payload,
            retry_at=retry_at,
            retry=retry,
            error_code=error_code,
            retry_class=retry_class,
            now=now,
        )
        return TaskSnapshot.from_record(row) if row is not None else None

    def recover_expired(self, *, now: float | None = None) -> dict[str, int]:
        return self.state.recover_expired(now=now)

    def events(
        self,
        *,
        consumer: str | None = None,
        limit: int = 100,
        include_consumed: bool = False,
    ) -> list[dict[str, Any]]:
        return self.state.list_task_events(
            consumer=consumer, limit=limit, include_consumed=include_consumed
        )

    def consume_event(
        self, event_id: int, consumer: str, *, now: float | None = None
    ) -> bool:
        return self.state.consume_task_event(event_id, consumer, now=now)

    def project_event(
        self,
        event_id: int,
        consumer: str,
        *,
        outbox_event: str | None = None,
        outbox_payload: Any = None,
        outbox_message_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        return self.state.project_task_event(
            event_id,
            consumer,
            outbox_event=outbox_event,
            outbox_payload=outbox_payload,
            outbox_message_id=outbox_message_id,
            now=now,
        )


__all__ = ["TaskStore"]
