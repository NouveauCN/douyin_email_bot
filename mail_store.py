"""Mail entry-point facade over the shared transactional state store.

The SQLite connection is deliberately still shared with the task store: mail
intake, task creation, event consumption, and SMTP outbox projection sometimes
need one transaction.  This module keeps those mail-shaped operations out of
the generic task service and gives callers a stable, explicit API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from download_types import validate_metadata
from mail_state import MailStateStore


class MailStore:
    """IMAP intake and SMTP projection facade."""

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

    def accept_message(
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
        return self._state.accept_message(
            mailbox,
            uidvalidity,
            uid,
            source_message_id,
            urls,
            metadata=validate_metadata(metadata),
            platform=platform,
            max_attempts=max_attempts,
            advance_position=advance_position,
            now=now,
        )

    def mark_intake_complete(self, source_message_id: str, *, now: float | None = None) -> bool:
        return self._state.mark_intake_complete(source_message_id, now=now)

    def ack_message(self, source_message_id: str, *, now: float | None = None) -> bool:
        return self._state.ack_message(source_message_id, now=now)

    def replace_source_metadata(
        self,
        source_message_id: str,
        metadata: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        return self._state.replace_source_metadata(
            source_message_id, validate_metadata(metadata), now=now
        )

    def get_mailbox_position(self, mailbox: str) -> dict[str, Any] | None:
        return self._state.get_mailbox_position(mailbox)

    def get_mailbox_generations(self, mailbox: str) -> list[dict[str, Any]]:
        return self._state.get_mailbox_generations(mailbox)

    def pending_seen(self, mailbox: str) -> list[dict[str, Any]]:
        return self._state.pending_seen(mailbox)

    def pending_intake(self, mailbox: str, uidvalidity: int) -> list[dict[str, Any]]:
        return self._state.pending_intake(mailbox, uidvalidity)

    def set_mailbox_position(
        self, mailbox: str, uidvalidity: int, last_uid: int, *, now: float | None = None
    ) -> dict[str, Any]:
        return self._state.set_mailbox_position(mailbox, uidvalidity, last_uid, now=now)

    def get_task(self, source_message_id: str, url: str) -> dict[str, Any] | None:
        return self._state.get_task(source_message_id, url)

    def get_task_by_id(self, task_id: int) -> dict[str, Any] | None:
        return self._state.get_task_by_id(task_id)

    def enqueue_notice(
        self,
        source_message_id: str,
        event: str,
        payload: Any,
        notification: Any,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._state.enqueue_notice(
            source_message_id, event, payload, notification, now=now
        )

    def events(
        self,
        *,
        consumer: str = "email",
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

    def consume_event(self, event_id: int, consumer: str = "email", *, now: float | None = None) -> bool:
        return self._state.consume_task_event(event_id, consumer, now=now)

    def project_event(
        self,
        event_id: int,
        consumer: str = "email",
        *,
        outbox_event: str | None = None,
        outbox_payload: Any = None,
        outbox_message_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Atomically create SMTP outbox work and ACK its task event."""
        return self._state.project_task_event(
            event_id,
            consumer,
            outbox_event=outbox_event,
            outbox_payload=outbox_payload,
            outbox_message_id=outbox_message_id,
            now=now,
        )

    def task_event_consumed(self, event_id: int, consumer: str = "email") -> bool:
        return self._state.task_event_consumed(event_id, consumer)

    def task_has_outbox(self, task_id: int) -> bool:
        return self._state.task_has_outbox(task_id)

    def unfinished_event_count(self, consumer: str = "email") -> int:
        return self._state.unfinished_event_count(consumer)

    def claim_outbox(
        self,
        limit: int = 1,
        *,
        lease_seconds: float | None = None,
        worker_id: str | None = None,
        filter: Any = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        return self._state.claim_outbox(
            limit,
            lease_seconds=lease_seconds,
            worker_id=worker_id,
            filter=filter,
            now=now,
        )

    def heartbeat_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        *,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        return self._state.heartbeat_outbox(
            outbox_id, lease_token, lease_seconds=lease_seconds, now=now
        )

    def mark_outbox_sent(self, outbox_id: int, lease_token: str, *, now: float | None = None) -> dict[str, Any] | None:
        return self._state.mark_outbox_sent(outbox_id, lease_token, now=now)

    def mark_outbox_failed(
        self,
        outbox_id: int,
        lease_token: str,
        error: str | None = None,
        *,
        retryable: bool = True,
        retry_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        return self._state.mark_outbox_failed(
            outbox_id,
            lease_token,
            error,
            retryable=retryable,
            retry_at=retry_at,
            now=now,
        )

    def recover_expired(self, *, now: float | None = None) -> dict[str, int]:
        return self._state.recover_expired(now=now)


__all__ = ["MailStore"]
