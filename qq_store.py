"""QQ C2C persistence facade.

The durable SQLite implementation remains in :mod:`mail_state` so the bot's
existing task workers can share one transaction and one lease database.  This
small facade gives the QQ bridge a source-specific API without exposing SMTP
tables or IMAP concepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from download_types import validate_metadata
from mail_state import MailStateStore


class QQStore:
    """QQ intake, reply outbox, and terminal-event projection facade."""

    stable_qq_msg_seq = staticmethod(MailStateStore.stable_qq_msg_seq)

    def __init__(self, state: MailStateStore | str | Path) -> None:
        if isinstance(state, MailStateStore):
            self._state = state
            self._owns_state = False
        else:
            self._state = MailStateStore(state)
            self._owns_state = True

    def close(self) -> None:
        if self._owns_state:
            self._state.close()

    def accept_qq_message(
        self,
        open_id: str,
        message_id: str,
        url: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        platform: str | None = None,
        max_attempts: int | None = None,
        confirmation_payload: Any = None,
        expires_at: float | None = None,
        reply_ttl: float = 3600.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically persist one QQ message, task, and accepted reply."""
        safe_metadata = dict(metadata or {})
        # MailStateStore performs the final validation too; keep this facade
        # explicit so all public intake paths reject secret-shaped context.
        return self._state.accept_qq_message(
            open_id,
            message_id,
            url,
            metadata=validate_metadata(safe_metadata),
            platform=platform,
            max_attempts=max_attempts,
            confirmation_payload=confirmation_payload,
            expires_at=expires_at,
            reply_ttl=reply_ttl,
            now=now,
        )

    def claim_qq_outbox(
        self,
        limit: int = 1,
        *,
        lease_seconds: float | None = None,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._state.claim_qq_outbox(
            limit, lease_seconds=lease_seconds, worker_id=worker_id, now=now
        )

    claim = claim_qq_outbox

    def heartbeat_qq_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        *,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        return self._state.heartbeat_qq_outbox(
            outbox_id, lease_token, lease_seconds=lease_seconds, now=now
        )

    heartbeat = heartbeat_qq_outbox

    def ack_qq_outbox(
        self, outbox_id: int, lease_token: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        return self._state.ack_qq_outbox(outbox_id, lease_token, now=now)

    ack = ack_qq_outbox

    def fail_qq_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        error: str | None = None,
        *,
        retryable: bool = True,
        retry_at: float | None = None,
        next_attempt_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        return self._state.fail_qq_outbox(
            outbox_id,
            lease_token,
            error,
            retryable=retryable,
            retry_at=retry_at,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    fail = fail_qq_outbox

    def expire_qq_outbox(self, *, now: float | None = None) -> int:
        return self._state.expire_qq_outbox(now=now)

    expire = expire_qq_outbox

    def project_terminal_event(
        self,
        event_id: int,
        *,
        consumer: str = "qq",
        payload: Any = None,
        expires_at: float | None = None,
        reply_ttl: float = 3600.0,
        now: float | None = None,
    ) -> bool:
        """Atomically create a terminal QQ reply and ACK its task event."""
        return self._state.project_qq_task_event(
            event_id,
            consumer=consumer,
            payload=payload,
            expires_at=expires_at,
            reply_ttl=reply_ttl,
            now=now,
        )

    project_qq_task_event = project_terminal_event

    def events(
        self,
        *,
        consumer: str = "qq",
        limit: int = 100,
        include_consumed: bool = False,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._state.list_task_events(
            consumer=consumer,
            limit=limit,
            include_consumed=include_consumed,
            after_id=after_id,
        )

    def consume_event(
        self, event_id: int, consumer: str = "qq", *, now: float | None = None
    ) -> bool:
        return self._state.consume_task_event(event_id, consumer, now=now)

    def get_task_by_id(self, task_id: int) -> dict[str, Any] | None:
        return self._state.get_task_by_id(task_id)

    def list_outbox(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._state.list_qq_outbox(status=status, limit=limit)

    def recover_expired(self, *, now: float | None = None) -> dict[str, int]:
        return self._state.recover_expired(now=now)

    def task_event_consumed(self, event_id: int, consumer: str = "qq") -> bool:
        return self._state.task_event_consumed(event_id, consumer)


QQStateStore = QQStore

__all__ = ["QQStateStore", "QQStore"]
