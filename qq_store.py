"""QQ C2C persistence facade.

The durable SQLite implementation remains in :mod:`mail_state` so the bot's
existing task workers can share one transaction and one lease database.  This
small facade gives the QQ bridge a source-specific API without exposing SMTP
tables or IMAP concepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mail_state import MailStateStore


class QQStore:
    """QQ intake, reply outbox, and terminal-event projection facade."""

    stable_qq_msg_seq = staticmethod(MailStateStore.stable_qq_msg_seq)

    def __init__(self, state: MailStateStore | str | Path) -> None:
        if isinstance(state, MailStateStore):
            self.state = state
            self._owns_state = False
        else:
            self.state = MailStateStore(state)
            self._owns_state = True

    def close(self) -> None:
        if self._owns_state:
            self.state.close()

    def accept_qq_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.state.accept_qq_message(*args, **kwargs)

    # Short names are convenient for the HTTP bridge and intentionally map to
    # the explicit methods on MailStateStore.
    def claim(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.state.claim_qq_outbox(*args, **kwargs)

    claim_qq_outbox = claim

    def heartbeat(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.state.heartbeat_qq_outbox(*args, **kwargs)

    heartbeat_qq_outbox = heartbeat

    def ack(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.state.ack_qq_outbox(*args, **kwargs)

    ack_qq_outbox = ack

    def fail(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.state.fail_qq_outbox(*args, **kwargs)

    fail_qq_outbox = fail

    def expire(self, *args: Any, **kwargs: Any) -> int:
        return self.state.expire_qq_outbox(*args, **kwargs)

    expire_qq_outbox = expire

    def project_terminal_event(self, *args: Any, **kwargs: Any) -> bool:
        return self.state.project_qq_task_event(*args, **kwargs)

    project_qq_task_event = project_terminal_event

    def events(
        self,
        *,
        consumer: str = "qq",
        limit: int = 100,
        include_consumed: bool = False,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.state.list_task_events(
            consumer=consumer,
            limit=limit,
            include_consumed=include_consumed,
            after_id=after_id,
        )


QQStateStore = QQStore

__all__ = ["QQStateStore", "QQStore"]
