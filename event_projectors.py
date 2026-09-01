"""Small channel-specific task event projectors.

Projectors deliberately own no threads and contain no persistence details.  The
bot supplies the callbacks used to format channel messages and to record the
legacy failure mirror; the channel facade performs the atomic outbox/ACK
transition.
"""

from __future__ import annotations

from typing import Any, Callable


TERMINAL_EVENTS = frozenset(
    {"task.succeeded", "task.partially_succeeded", "task.failed"}
)


class EmailEventProjector:
    """Project task events into the SMTP outbox through :class:`MailStore`."""

    def __init__(
        self,
        mail_store,
        *,
        failure_projector: Callable[[dict, dict], bool],
        notification_builder: Callable[[dict, dict], dict | None],
        notifications_enabled: Callable[[], bool],
    ) -> None:
        self.mail_store = mail_store
        self.failure_projector = failure_projector
        self.notification_builder = notification_builder
        self.notifications_enabled = notifications_enabled

    def project_once(self, *, limit: int = 25) -> bool:
        """Project at most ``limit`` pending events; return whether work existed."""
        events = self.mail_store.events(consumer="email", limit=limit)
        for event in events:
            event_id = int(event["id"])
            task = self.mail_store.get_task_by_id(int(event["task_id"]))
            if task is None or str(event.get("event_type") or "") not in TERMINAL_EVENTS:
                self.mail_store.consume_event(event_id, "email")
                continue
            if not self.failure_projector(task, event):
                continue
            if not self.notifications_enabled():
                self.mail_store.consume_event(event_id, "email")
                continue
            task_id = int(task["id"])
            if self.mail_store.task_has_outbox(task_id):
                self.mail_store.consume_event(event_id, "email")
                continue
            notification = self.notification_builder(task, event)
            if notification is None:
                self.mail_store.consume_event(event_id, "email")
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            result = payload.get("result") or task.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            event_type = str(event.get("event_type") or "")
            outbox_event = (
                "partial-failed"
                if result.get("partial")
                else "failed"
                if event_type == "task.failed"
                else "completed"
            )
            self.mail_store.project_event(
                event_id,
                "email",
                outbox_event=outbox_event,
                outbox_payload=notification,
            )
        return bool(events)


class QQEventProjector:
    """Project task events into the passive QQ reply outbox."""

    def __init__(self, qq_store, *, notification_builder: Callable[[dict, dict], str]):
        self.qq_store = qq_store
        self.notification_builder = notification_builder

    def project_once(self, *, limit: int = 25) -> bool:
        events = self.qq_store.events(consumer="qq", limit=limit)
        for event in events:
            event_id = int(event["id"])
            event_type = str(event.get("event_type") or "")
            if event_type not in TERMINAL_EVENTS:
                self.qq_store.consume_event(event_id, "qq")
                continue
            task = self.qq_store.get_task_by_id(int(event["task_id"]))
            if task is None:
                continue
            self.qq_store.project_terminal_event(
                event_id,
                payload={"body": self.notification_builder(task, event)},
            )
        return bool(events)


__all__ = ["EmailEventProjector", "QQEventProjector", "TERMINAL_EVENTS"]
