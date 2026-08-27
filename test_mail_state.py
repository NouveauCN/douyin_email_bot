import pytest

from mail_state import MailStateStore, SCHEMA_VERSION


def make_store(tmp_path):
    return MailStateStore(tmp_path / "mail-state.sqlite", default_lease_seconds=10)


def test_schema_is_initialized_with_wal_and_busy_timeout(tmp_path):
    store = make_store(tmp_path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    store.close()


def test_duplicate_intake_and_urls_are_idempotent(tmp_path):
    store = make_store(tmp_path)
    first = store.accept_message(
        "INBOX", 7, 11, "<message-1>",
        ["HTTPS://EXAMPLE.test/post#tracking", "https://example.test/post"],
    )
    second = store.accept_message("INBOX", 7, 11, "<message-1>", ["https://example.test/post"])
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["task_ids"] == second["task_ids"]
    assert second["created_task_ids"] == []
    assert store.get_mailbox_position("INBOX")["last_uid"] == 11
    store.close()


def test_uidvalidity_change_archives_old_generation_and_resets_position(tmp_path):
    store = make_store(tmp_path)
    store.accept_message("INBOX", 7, 99, "m1", [])
    store.accept_message("INBOX", 8, 3, "m2", [])
    assert store.get_mailbox_position("INBOX")["uidvalidity"] == 8
    assert store.get_mailbox_position("INBOX")["last_uid"] == 3
    assert store.get_mailbox_generations("INBOX")[0]["uidvalidity"] == 7
    assert store.get_mailbox_generations("INBOX")[0]["last_uid"] == 99
    store.close()


def test_task_lease_heartbeat_completion_and_expiry_recovery(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    claimed = store.claim_tasks(now=100)[0]
    assert claimed["attempts"] == 1
    assert store.heartbeat_task(task["id"], claimed["lease_token"], now=105)["lease_expires_at"] == 115
    assert store.recover_expired(now=114)["tasks"] == 0
    assert store.recover_expired(now=115)["tasks"] == 1
    reclaimed = store.claim_tasks(now=116)[0]
    assert reclaimed["attempts"] == 2
    assert store.complete_task(
        task["id"], reclaimed["lease_token"], result={"ok": True}, now=117
    )["status"] == "succeeded"
    assert store.claim_tasks(now=200) == []
    store.close()


def test_complete_task_can_atomically_enqueue_notification(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a", platform="douyin")
    claimed = store.claim_tasks(platform="douyin", now=100)[0]
    completed = store.complete_task(
        task["id"],
        claimed["lease_token"],
        result={"path": "/downloads/a.mp4"},
        outbox_event="success",
        outbox_payload={"to": "user@example.test"},
        now=101,
    )
    assert completed["status"] == "succeeded"
    outbox = store._conn.execute("SELECT status, payload_json FROM smtp_outbox").fetchone()
    assert outbox["status"] == "pending"
    assert "user@example.test" in outbox["payload_json"]
    store.close()


def test_outbox_is_unique_message_id_stable_and_filterable(tmp_path):
    store = make_store(tmp_path)
    douyin = store.enqueue_task("m1", "https://example.test/a", platform="douyin")
    bilibili = store.enqueue_task("m2", "https://example.test/b", platform="bilibili")
    first = store.enqueue_outbox(douyin["id"], "completed", {"subject": "done"})
    duplicate = store.enqueue_outbox(
        douyin["id"], "completed", {"subject": "changed"}, message_id="<different@example.test>"
    )
    store.enqueue_outbox(bilibili["id"], "completed")
    assert first["id"] == duplicate["id"]
    assert first["message_id"] == duplicate["message_id"]
    item = store.claim_outbox(platform="douyin", now=100)[0]
    assert item["attempts"] == 1
    assert store.mark_outbox_failed(
        item["id"], item["lease_token"], "SMTP down", retry_at=110, now=101
    )["status"] == "failed"
    assert store.claim_outbox(platform="douyin", now=109) == []
    retry = store.claim_outbox(now=110, filter={"platform": "douyin"})[0]
    assert retry["message_id"] == first["message_id"]
    assert store.mark_outbox_sent(retry["id"], retry["lease_token"], now=111)["status"] == "sent"
    store.close()


def test_exception_rolls_back_entire_intake_transaction(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original = store._insert_task_locked

    def fail_on_insert(*args, **kwargs):
        raise RuntimeError("fault injection")

    monkeypatch.setattr(store, "_insert_task_locked", fail_on_insert)
    with pytest.raises(RuntimeError, match="fault injection"):
        store.accept_message("INBOX", 7, 1, "m1", ["https://example.test/a"])
    monkeypatch.setattr(store, "_insert_task_locked", original)
    assert store.get_mailbox_position("INBOX") is None
    assert store._conn.execute("SELECT COUNT(*) FROM source_messages").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    store.close()


def test_expired_outbox_lease_is_recoverable(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    item = store.enqueue_outbox(task["id"], "failed")
    claimed = store.claim_outbox(now=0, lease_seconds=5)[0]
    assert claimed["id"] == item["id"]
    assert store.recover_expired(now=5) == {"tasks": 0, "outbox": 1}
    assert store.claim_outbox(now=6)[0]["id"] == item["id"]
    store.close()


def test_invalid_json_payload_rolls_back_outbox_insert(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    with pytest.raises(TypeError):
        store.enqueue_outbox(task["id"], "completed", {"bad": object()})
    assert store._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
    store.close()


def test_notification_insert_failure_rolls_back_task_completion(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    claimed = store.claim_tasks(now=100)[0]

    def fail_outbox(*args, **kwargs):
        raise RuntimeError("outbox fault")

    monkeypatch.setattr(store, "_insert_outbox_locked", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox fault"):
        store.complete_task(
            task["id"], claimed["lease_token"], notification={"subject": "done"}, now=101
        )
    row = store._conn.execute("SELECT status, lease_token FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    assert row["status"] == "leased"
    assert row["lease_token"] == claimed["lease_token"]
    assert store._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
    store.close()
