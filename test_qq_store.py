from mail_state import MailStateStore
from qq_store import QQStore


def test_accept_qq_message_is_atomic_and_idempotent(tmp_path):
    state = MailStateStore(tmp_path / "state.sqlite")
    first = state.accept_qq_message("openid", "message", "https://v.douyin.com/abc", now=0)
    duplicate = state.accept_qq_message("openid", "message", "https://v.douyin.com/abc", now=1)

    assert first["task_id"] == duplicate["task_id"]
    assert duplicate["duplicate"] is True
    assert state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert state._conn.execute("SELECT COUNT(*) FROM qq_task_bindings").fetchone()[0] == 1
    assert state._conn.execute("SELECT COUNT(*) FROM qq_outbox").fetchone()[0] == 1
    assert first["outbox"]["msg_seq"] == 1
    state.close()


def test_qq_outbox_delivery_and_terminal_projection(tmp_path):
    state = MailStateStore(tmp_path / "state.sqlite")
    accepted = state.accept_qq_message("openid", "message", "fake:1", now=0)
    claimed = state.claim_qq_outbox(now=1, lease_seconds=10)[0]
    state.ack_qq_outbox(claimed["id"], claimed["lease_token"], now=2)

    task = state.claim_tasks(now=2, lease_seconds=10)[0]
    state.complete_task(task["id"], task["lease_token"], result={"success": True}, now=3)
    event = state.list_task_events(consumer="qq")[0]
    assert state.project_qq_task_event(event["id"], now=4) is True
    assert state.project_qq_task_event(event["id"], now=4) is False
    terminal = state.claim_qq_outbox(now=5)[0]
    assert terminal["event"] == "completed"
    assert terminal["msg_seq"] == 2
    assert state.ack_qq_outbox(terminal["id"], terminal["lease_token"], now=6)["status"] == "sent"
    state.close()


def test_terminal_reply_keeps_original_passive_deadline(tmp_path):
    state = MailStateStore(tmp_path / "state.sqlite")
    accepted = state.accept_qq_message(
        "openid", "message", "fake:1", reply_ttl=10, now=0
    )
    ack = state.claim_qq_outbox(now=1)[0]
    state.ack_qq_outbox(ack["id"], ack["lease_token"], now=2)
    task = state.claim_tasks(now=2)[0]
    state.complete_task(task["id"], task["lease_token"], result={"success": True}, now=3)
    event = state.list_task_events(consumer="qq")[0]
    state.project_qq_task_event(event["id"], now=9)

    terminal = state.list_qq_outbox(limit=10)[1]
    assert terminal["expires_at"] == accepted["outbox"]["expires_at"]
    assert state.claim_qq_outbox(now=11) == []
    assert state.list_qq_outbox(status="expired")[0]["id"] == terminal["id"]
    state.close()


def test_email_event_reader_excludes_qq_tasks(tmp_path):
    state = MailStateStore(tmp_path / "state.sqlite")
    accepted = state.accept_qq_message("openid", "message", "fake:1", now=0)
    task = state.claim_tasks(now=1)[0]
    state.complete_task(task["id"], task["lease_token"], result={"success": True}, now=2)

    assert state.list_task_events(consumer="email") == []
    assert len(state.list_task_events(consumer="qq")) == 1
    assert state.unfinished_event_count("qq") == 1
    state.close()


def test_qq_delivery_failure_uses_server_side_retry_policy(tmp_path):
    state = MailStateStore(tmp_path / "state.sqlite")
    state.accept_qq_message("openid", "message", "fake:1", now=0)
    claim = state.claim_qq_outbox(now=1)[0]

    failed = state.fail_qq_outbox(
        claim["id"], claim["lease_token"], "network", retryable=True, now=2
    )
    assert failed["status"] == "failed"
    assert failed["next_attempt_at"] == 62
    state.close()


def test_qq_claim_recovers_lease_but_expires_passive_window(tmp_path):
    store = QQStore(tmp_path / "state.sqlite")
    assert not hasattr(store, "state")
    item = store.accept_qq_message(
        "openid", "message", "fake:1", reply_ttl=10, now=0
    )["outbox"]
    claim = store.claim(now=1, lease_seconds=2)[0]
    assert store.claim(now=3, lease_seconds=2) == []
    store.recover_expired(now=3)
    assert store.claim(now=3, lease_seconds=2)[0]["id"] == item["id"]
    store.recover_expired(now=11)
    assert store.claim(now=11) == []
    store.close()


def test_qq_facade_rejects_secret_metadata(tmp_path):
    store = QQStore(tmp_path / "state.sqlite")
    try:
        store.accept_qq_message("openid", "message", "fake:1", metadata={"token": "x"})
    except ValueError as exc:
        assert "metadata" in str(exc)
    else:
        raise AssertionError("secret metadata was accepted")
    store.close()
