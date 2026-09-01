from __future__ import annotations

import pytest

from qq_bridge import QQBridgeServer, create_app


class Handler:
    def __init__(self):
        self.messages = []
        self.failed = []

    def accept_qq_message(self, payload):
        self.messages.append(payload)
        return {"accepted": True, "task_ids": [7]}

    def claim_qq_outbox(self, **kwargs):
        assert kwargs == {"limit": 10, "worker_id": "gateway"}
        return [{"id": 3, "content": "done"}]

    def ack_qq_outbox(self, outbox_id, lease_token):
        assert (outbox_id, lease_token) == (3, "lease")
        return {"id": 3, "status": "sent"}

    def fail_qq_outbox(self, outbox_id, lease_token, error, *, retryable=True):
        self.failed.append((outbox_id, lease_token, error, retryable))
        return {"id": outbox_id, "status": "failed"}


def test_health_is_public_but_protected_routes_require_bearer():
    handler = Handler()
    client = create_app(handler, token="bridge-secret").test_client()

    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert client.post("/v1/qq/messages", json={}).status_code == 401


def test_message_claim_ack_and_fail_routes():
    handler = Handler()
    client = create_app(handler, token="bridge-secret").test_client()
    headers = {"Authorization": "Bearer bridge-secret"}

    response = client.post(
        "/v1/qq/messages",
        json={"open_id": "u", "message_id": "m", "text": "https://v.douyin.com/x"},
        headers=headers,
    )
    assert response.status_code == 202
    assert handler.messages[0]["message_id"] == "m"

    response = client.post(
        "/v1/qq/outbox/claim",
        json={"limit": 10, "worker_id": "gateway", "lease_seconds": 999999},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["items"][0]["id"] == 3

    response = client.post(
        "/v1/qq/outbox/3/ack", json={"lease_token": "lease"}, headers=headers
    )
    assert response.status_code == 200

    response = client.post(
        "/v1/qq/outbox/3/fail",
        json={"lease_token": "lease", "error": "network", "retry_at": 0},
        headers=headers,
    )
    assert response.status_code == 200
    assert handler.failed == [(3, "lease", "network", True)]


def test_missing_token_fails_closed_and_claim_limit_is_bounded():
    handler = Handler()
    client = create_app(handler, token="").test_client()
    response = client.post("/v1/qq/outbox/claim", json={"limit": 1})
    assert response.status_code == 401

    client = create_app(handler, token="secret").test_client()
    response = client.post(
        "/v1/qq/outbox/claim",
        json={"limit": 101},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 400


def test_server_refuses_to_listen_without_a_token():
    server = QQBridgeServer(Handler(), token="", port=0)
    with pytest.raises(RuntimeError, match="QQ_BRIDGE_TOKEN"):
        server.start()
