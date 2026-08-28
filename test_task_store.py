from download_types import SourceRef, TaskRequest, TaskStatus
from task_store import TaskStore


def test_submit_is_idempotent_per_source_namespace(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite")
    first = store.submit(TaskRequest("https://example.test/video", SourceRef("email", "1")))
    duplicate = store.submit(TaskRequest("https://example.test/video", SourceRef("email", "1")))
    other_entry = store.submit(TaskRequest("https://example.test/video", SourceRef("qq", "1")))
    assert duplicate.task_id == first.task_id
    assert other_entry.task_id != first.task_id
    assert first.source == SourceRef("email", "1")
    store.close()


def test_task_lifecycle_and_event_projection_are_durable(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite")
    submitted = store.submit(
        TaskRequest("fake:1", SourceRef("qq", "1"), {"reply_to": "user@example.test"}),
        platform="fake",
        max_attempts=2,
    )
    claimed = store.claim(now=100, lease_seconds=10)[0]
    assert claimed.status == TaskStatus.RUNNING
    completed = store.complete(
        claimed.task_id,
        claimed.lease_token,
        {"success": True, "filepath": "/tmp/a.mp4", "files": ["/tmp/a.mp4"]},
        now=101,
    )
    assert completed is not None
    assert completed.status == TaskStatus.SUCCEEDED
    events = store.events()
    assert [event["event_type"] for event in events] == ["task.succeeded"]
    assert store.project_event(
        events[0]["id"],
        "qq-notifier",
        outbox_event="completed",
        outbox_payload={"to_addr": "user@example.test", "body": "done"},
    ) is True
    assert store.project_event(events[0]["id"], "qq-notifier") is False
    assert store.state._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 1
    store.close()


def test_expired_lease_is_recoverable(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite")
    store.submit(TaskRequest("fake:1", SourceRef("qq", "1")))
    claimed = store.claim(now=100, lease_seconds=10)[0]
    assert claimed.status == TaskStatus.RUNNING
    assert store.recover_expired(now=111)["tasks"] == 1
    assert store.claim(now=111)[0].attempts == 2
    store.close()
