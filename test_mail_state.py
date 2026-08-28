import json
import sqlite3

import pytest

from mail_state import MailStateStore, SCHEMA_VERSION


def make_store(tmp_path):
    return MailStateStore(tmp_path / "mail-state.sqlite", default_lease_seconds=10)


def make_legacy_store_file(path):
    conn = sqlite3.connect(path)
    secret = "sessionid=" + "x" * 128
    conn.executescript(
        """
        CREATE TABLE source_messages (
            source_message_id TEXT PRIMARY KEY,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            seen_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            platform TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT,
            lease_expires_at REAL,
            next_attempt_at REAL,
            last_error TEXT,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            UNIQUE (source_message_id, normalized_url)
        );
        CREATE TABLE smtp_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL,
            lease_token TEXT,
            lease_expires_at REAL,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            sent_at REAL,
            UNIQUE (task_id, event)
        );
        PRAGMA user_version = 1;
        """
    )
    conn.execute(
        "INSERT INTO source_messages VALUES (?, ?, ?, ?, '{}', NULL, ?, ?)",
        ("legacy-orphan", "INBOX", 77, 12, 1, 1),
    )
    conn.execute(
        "INSERT INTO source_messages VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            "legacy-cookie",
            "INBOX",
            77,
            13,
            json.dumps({"subject": "更新cookie " + secret}),
            1,
            1,
        ),
    )
    conn.execute(
        "INSERT INTO source_messages VALUES (?, ?, ?, ?, '{}', NULL, ?, ?)",
        ("legacy-cookie-pending", "INBOX", 77, 14, 1, 1),
    )
    cursor = conn.execute(
        "INSERT INTO tasks "
        "(source_message_id, normalized_url, original_url, platform, payload_json, created_at, updated_at) "
        "VALUES (?, ?, ?, 'cookie', ?, ?, ?)",
        (
            "legacy-cookie",
            "urn:mail-command:cookie_update",
            "urn:mail-command:cookie_update",
            json.dumps(
                {
                    "sender": "user@example.test",
                    "subject": "更新cookie " + secret,
                    "command": "cookie_update",
                    "body": secret,
                }
            ),
            1,
            1,
        ),
    )
    conn.execute(
        "UPDATE tasks SET result_json = ?, last_error = ? WHERE id = ?",
        (json.dumps({"cookie": secret}), secret, cursor.lastrowid),
    )
    conn.execute(
        "INSERT INTO smtp_outbox "
        "(task_id, event, message_id, payload_json, last_error, created_at, updated_at) "
        "VALUES (?, 'completed', '<legacy@example.test>', ?, ?, 1, 1)",
        (
            cursor.lastrowid,
            json.dumps({"to_addr": "user@example.test", "body": secret}),
            secret,
        ),
    )
    conn.execute(
        "INSERT INTO tasks "
        "(source_message_id, normalized_url, original_url, platform, payload_json, created_at, updated_at) "
        "VALUES (?, ?, ?, 'cookie', ?, ?, ?)",
        (
            "legacy-cookie-pending",
            "urn:mail-command:cookie_auto",
            "urn:mail-command:cookie_auto",
            json.dumps(
                {
                    "sender": "user@example.test",
                    "subject": "自动获取cookie",
                    "command": "cookie_auto",
                    "body": secret,
                }
            ),
            1,
            1,
        ),
    )
    leased_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE tasks SET status = 'leased', lease_token = 'legacy-token', "
        "lease_expires_at = 99, result_json = ?, last_error = ? WHERE id = ?",
        (json.dumps({"cookie": secret}), secret, leased_task_id),
    )
    conn.commit()
    conn.close()
    return secret


def test_schema_is_initialized_with_wal_and_busy_timeout(tmp_path):
    store = make_store(tmp_path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    store.close()


def test_legacy_schema_upgrade_recovers_incomplete_intake_and_scrubs_cookie(tmp_path):
    state_path = tmp_path / "legacy.sqlite"
    secret = make_legacy_store_file(state_path)

    store = MailStateStore(state_path)

    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.pending_intake("INBOX", 77)[0]["source_message_id"] == "legacy-orphan"
    assert [item["source_message_id"] for item in store.pending_seen("INBOX")] == [
        "legacy-cookie",
        "legacy-cookie-pending",
    ]
    task = store._conn.execute(
        "SELECT status, payload_json, result_json, last_error FROM tasks "
        "WHERE source_message_id = 'legacy-cookie'"
    ).fetchone()
    outbox = store._conn.execute(
        "SELECT payload_json FROM smtp_outbox WHERE task_id = 1"
    ).fetchone()
    assert task["status"] == "failed"
    assert task["payload_json"] == "{}"
    assert secret not in task["payload_json"]
    assert task["result_json"] is None
    assert task["last_error"] is None
    assert secret not in outbox["payload_json"]
    assert outbox["payload_json"].find(secret) == -1
    metadata = store._conn.execute(
        "SELECT metadata_json FROM source_messages "
        "WHERE source_message_id = 'legacy-cookie'"
    ).fetchone()
    assert secret not in metadata["metadata_json"]
    recovered = store._conn.execute(
        "SELECT t.status, t.lease_token, t.lease_expires_at, t.result_json, "
        "t.last_error, o.event, o.payload_json FROM tasks t "
        "LEFT JOIN smtp_outbox o ON o.task_id = t.id "
        "WHERE t.source_message_id = 'legacy-cookie-pending'"
    ).fetchone()
    assert recovered["status"] == "failed"
    assert recovered["lease_token"] is None
    assert recovered["lease_expires_at"] is None
    assert recovered["result_json"] is None
    assert recovered["last_error"] is None
    assert recovered["event"] == "legacy-cookie-recovery"
    assert secret not in recovered["payload_json"]
    assert recovered["payload_json"] == json.dumps(
        {
            "body": "旧版 Cookie 任务无法安全恢复，请通过 Web Login 或 uv run python get_cookie.py 更新 Cookie。",
            "subject_status": "Cookie 需通过 Web Login 更新",
            "to_addr": "user@example.test",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    store.close()
    for artifact in (state_path, state_path.with_name(state_path.name + "-wal")):
        if artifact.exists():
            assert secret.encode() not in artifact.read_bytes()


def test_legacy_migration_retries_when_physical_cleanup_fails(tmp_path, monkeypatch):
    state_path = tmp_path / "legacy-retry.sqlite"
    make_legacy_store_file(state_path)
    original_cleanup = MailStateStore._secure_migrate_cleanup
    calls = []

    def fail_once(store):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("simulated cleanup failure")
        return original_cleanup(store)

    monkeypatch.setattr(MailStateStore, "_secure_migrate_cleanup", fail_once)
    with pytest.raises(RuntimeError, match="simulated cleanup failure"):
        MailStateStore(state_path)

    with sqlite3.connect(state_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    store = MailStateStore(state_path)
    assert calls == [True, True]
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    recipients = store._conn.execute(
        "SELECT o.payload_json FROM smtp_outbox o "
        "JOIN tasks t ON t.id = o.task_id "
        "WHERE t.platform = 'cookie' ORDER BY o.id"
    ).fetchall()
    assert recipients
    assert all('"to_addr": "user@example.test"' in row["payload_json"] for row in recipients)
    store.close()


def test_busy_wal_checkpoint_result_is_rejected():
    class BusyCursor:
        def fetchone(self):
            return (1, 3, 3)

    class BusyConnection:
        def execute(self, statement):
            assert statement == "PRAGMA wal_checkpoint(TRUNCATE)"
            return BusyCursor()

    class BusyStore:
        _conn = BusyConnection()

    with pytest.raises(RuntimeError, match="checkpoint is busy"):
        MailStateStore._checkpoint_wal(BusyStore())


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


def test_accept_message_can_persist_without_advancing_mailbox_position(tmp_path):
    store = make_store(tmp_path)
    accepted = store.accept_message(
        "INBOX",
        77,
        12,
        "INBOX:77:12",
        ["https://example.test/post"],
        advance_position=False,
    )

    assert accepted["position"] is None
    assert store.get_mailbox_position("INBOX") is None
    assert store.pending_intake("INBOX", 77)[0]["uid"] == 12
    store.close()


def test_source_metadata_can_be_replaced_for_quarantine(tmp_path):
    store = make_store(tmp_path)
    store.accept_message(
        "INBOX",
        7,
        11,
        "m1",
        [],
        metadata={"sender": "user@example.test", "subject": "private subject"},
    )

    assert store.replace_source_metadata(
        "m1",
        {"uid": 11, "raw_sha256": "hash", "intake_error": "ValueError"},
    ) is True
    row = store._conn.execute(
        "SELECT metadata_json FROM source_messages WHERE source_message_id = ?",
        ("m1",),
    ).fetchone()
    assert row["metadata_json"] == (
        '{"intake_error": "ValueError", "raw_sha256": "hash", "uid": 11}'
    )
    assert store.replace_source_metadata("missing", {}) is False
    store.close()


def test_intake_marker_precedes_seen_ack_and_rollback_drain(tmp_path):
    store = make_store(tmp_path)
    accepted = store.accept_message("INBOX", 7, 11, "m1", [])
    assert accepted["intake_complete"] is False
    assert store.pending_seen("INBOX") == []
    assert store.pending_intake("INBOX", 7)[0]["uid"] == 11
    assert store.unfinished_work_counts()["intake"] == 1

    assert store.mark_intake_complete("m1") is True
    assert store.pending_seen("INBOX")[0]["source_message_id"] == "m1"
    assert store.unfinished_work_counts()["intake"] == 1
    assert store.ack_message("m1") is True
    assert store.unfinished_work_counts()["intake"] == 0
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


def test_capacity_release_does_not_charge_a_task_attempt(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    claimed = store.claim_tasks(now=100)[0]
    released = store.release_task(
        task["id"], claimed["lease_token"], next_attempt_at=101, now=100
    )
    assert released["status"] == "pending"
    assert released["attempts"] == 0
    assert store.claim_tasks(now=100) == []
    assert store.claim_tasks(now=101)[0]["attempts"] == 1
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


def test_notice_and_outbox_insert_roll_back_together(tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def fail_outbox(*args, **kwargs):
        raise RuntimeError("notice outbox fault")

    monkeypatch.setattr(store, "_insert_outbox_locked", fail_outbox)
    with pytest.raises(RuntimeError, match="notice outbox fault"):
        store.enqueue_notice(
            "m-notice",
            "no-url",
            {"sender": "user@example.test"},
            {"to_addr": "user@example.test", "body": "no URL"},
        )

    assert store._conn.execute("SELECT COUNT(*) FROM source_messages").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
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


def test_outbox_heartbeat_prevents_expiry_during_slow_smtp(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    item = store.enqueue_outbox(task["id"], "completed")
    claimed = store.claim_outbox(now=0, lease_seconds=5)[0]
    assert store.heartbeat_outbox(
        item["id"], claimed["lease_token"], now=4, lease_seconds=5
    )["lease_expires_at"] == 9
    assert store.recover_expired(now=8)["outbox"] == 0
    assert store.mark_outbox_sent(item["id"], claimed["lease_token"], now=8)
    store.close()


def test_terminal_failed_task_and_sent_outbox_do_not_block_rollback(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    claimed_task = store.claim_tasks(now=100)[0]
    store.fail_task(task["id"], claimed_task["lease_token"], "permanent", now=101)
    assert store.unfinished_work_counts()["tasks"] == 0

    task2 = store.enqueue_task("m2", "https://example.test/b")
    claimed_task2 = store.claim_tasks(now=100)[0]
    store.complete_task(task2["id"], claimed_task2["lease_token"], result={"ok": True}, now=101)
    outbox = store.enqueue_outbox(task2["id"], "failed")
    claimed_outbox = store.claim_outbox(now=100)[0]
    store.mark_outbox_sent(outbox["id"], claimed_outbox["lease_token"], now=101)
    assert store.unfinished_work_counts() == {"intake": 0, "tasks": 0, "outbox": 0}
    store.close()


def test_undelivered_failed_outbox_blocks_rollback(tmp_path):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    outbox = store.enqueue_outbox(task["id"], "failed")
    claimed_outbox = store.claim_outbox(now=100)[0]
    store.mark_outbox_failed(
        outbox["id"], claimed_outbox["lease_token"], "permanent", now=101
    )
    store.close()


@pytest.mark.parametrize(
    "payload_json", ['{', '[]', '{"legacy_retry_key": 123}']
)
def test_strict_terminal_legacy_keys_reject_corrupt_payloads(tmp_path, payload_json):
    store = make_store(tmp_path)
    task = store.enqueue_task("m1", "https://example.test/a")
    claimed = store.claim_tasks(now=100)[0]
    store.complete_task(task["id"], claimed["lease_token"], result={"ok": True}, now=101)
    store._conn.execute(
        "UPDATE tasks SET payload_json = ? WHERE id = ?", (payload_json, task["id"])
    )

    with pytest.raises(RuntimeError, match="terminal task"):
        store.terminal_legacy_retry_keys(strict=True)
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
