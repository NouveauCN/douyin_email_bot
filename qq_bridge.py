"""Small, authenticated HTTP bridge for the QQ gateway process.

The QQ Open Platform client lives in a separate process.  This module keeps
that process deliberately thin: it posts inbound messages here and polls a
durable outbox for replies.  The bridge has no download or QQ SDK logic and
can therefore be exercised with Flask's test client.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from flask import Flask, g, jsonify, request
from werkzeug.serving import BaseWSGIServer, make_server
from werkzeug.exceptions import RequestEntityTooLarge

logger = logging.getLogger("QQBridge")


def _json_result(value: Any) -> Any:
    """Convert common store return values to a JSON-safe response value."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_result(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_result(value.to_dict())
    if hasattr(value, "__dict__"):
        return _json_result(vars(value))
    return value


def _request_json() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _validate_message_payload(payload: dict[str, Any]) -> None:
    """Apply cheap protocol limits before invoking the bot/store callback."""
    open_id = payload.get("open_id", payload.get("sender_id"))
    message_id = payload.get("message_id")
    text = payload.get("text", payload.get("content", ""))
    if not isinstance(open_id, str) or not open_id.strip() or len(open_id) > 256:
        raise ValueError("open_id is invalid")
    if any(char.isspace() for char in open_id):
        raise ValueError("open_id is invalid")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 1024:
        raise ValueError("message_id is invalid")
    if any(char.isspace() for char in message_id):
        raise ValueError("message_id is invalid")
    if not isinstance(text, str) or len(text) > 64 * 1024:
        raise ValueError("text is invalid")


def _lease_token(payload: dict[str, Any]) -> str:
    value = payload.get("lease_token")
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("lease_token is invalid")
    if any(char.isspace() for char in value):
        raise ValueError("lease_token is invalid")
    return value.strip()


def create_app(
    handler: Any,
    *,
    token: str | None = None,
    token_env: str = "QQ_BRIDGE_TOKEN",
) -> Flask:
    """Create the internal bridge application.

    ``handler`` is intentionally a small duck-typed object.  It must expose
    ``accept_qq_message``, ``claim_qq_outbox``, ``ack_qq_outbox`` and
    ``fail_qq_outbox``.  Keeping those calls at the boundary lets the durable
    SQLite implementation evolve without coupling the HTTP process to it.
    """

    configured_token = token if token is not None else os.getenv(token_env, "")
    app = Flask("qq_bridge")
    # QQ messages are short text payloads.  Keeping this limit at the HTTP
    # edge prevents an accidental large request from reaching SQLite/logs.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.config["QQ_BRIDGE_TOKEN"] = str(configured_token or "")

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_error: RequestEntityTooLarge) -> Any:
        return jsonify({"error": "request body is too large"}), 413

    @app.before_request
    def _authenticate() -> Any:
        # Health is used by Compose and intentionally does not reveal any
        # configuration or credential state.
        if request.path == "/health":
            return None
        expected = app.config["QQ_BRIDGE_TOKEN"]
        authorization = request.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.before_request
    def _track_request() -> None:
        # Track only authenticated bridge requests so shutdown can account for
        # handlers still using the shared SQLite connection.
        if request.path != "/health":
            callback = getattr(handler, "qq_request_started", None)
            if callable(callback):
                callback()
            g.qq_bridge_tracked = True

    @app.teardown_request
    def _finish_request(_error: BaseException | None) -> None:
        if getattr(g, "qq_bridge_tracked", False):
            callback = getattr(handler, "qq_request_finished", None)
            if callable(callback):
                callback()

    @app.get("/health")
    def health() -> Any:
        try:
            callback = getattr(handler, "qq_health", None)
            if callable(callback):
                callback()
            # Do not disclose database paths, credentials, queue depth, or
            # runtime configuration through a probe endpoint.
            return jsonify({"status": "ok"})
        except Exception:
            logger.exception("QQ bridge health check failed")
            return jsonify({"status": "degraded"}), 503

    @app.post("/v1/qq/messages")
    def accept_message() -> Any:
        try:
            payload = _request_json()
            _validate_message_payload(payload)
            result = handler.accept_qq_message(payload)
            return jsonify(_json_result(result)), 202
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("QQ message acceptance failed")
            return jsonify({"error": "message acceptance failed"}), 503

    @app.post("/v1/qq/outbox/claim")
    def claim_outbox() -> Any:
        try:
            payload = request.get_json(silent=True)
            payload = payload if isinstance(payload, dict) else {}
            limit = int(payload.get("limit", 10))
            if limit < 1 or limit > 100:
                raise ValueError("limit must be between 1 and 100")
            result = handler.claim_qq_outbox(
                limit=limit,
                worker_id=payload.get("worker_id"),
            )
            return jsonify({"items": _json_result(result)}), 200
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("QQ outbox claim failed")
            return jsonify({"error": "outbox claim failed"}), 503

    @app.post("/v1/qq/outbox/<int:outbox_id>/ack")
    def ack_outbox(outbox_id: int) -> Any:
        try:
            payload = _request_json()
            lease_token = _lease_token(payload)
            result = handler.ack_qq_outbox(outbox_id, lease_token)
            if result is None or result is False:
                return jsonify({"error": "outbox item is not leased"}), 409
            return jsonify({"item": _json_result(result)}), 200
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("QQ outbox acknowledgement failed")
            return jsonify({"error": "outbox acknowledgement failed"}), 503

    @app.post("/v1/qq/outbox/<int:outbox_id>/fail")
    def fail_outbox(outbox_id: int) -> Any:
        try:
            payload = _request_json()
            lease_token = _lease_token(payload)
            error = str(payload.get("error") or "QQ delivery failed")[:1000]
            # Retry timing and attempt limits are owned by the durable store;
            # callers cannot use this endpoint to extend a lease or bypass
            # server-side backoff.
            retryable = payload.get("retryable", True)
            if not isinstance(retryable, bool):
                raise ValueError("retryable must be a boolean")
            result = handler.fail_qq_outbox(
                outbox_id, lease_token, error, retryable=retryable
            )
            if result is None or result is False:
                return jsonify({"error": "outbox item is not leased"}), 409
            return jsonify({"item": _json_result(result)}), 200
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("QQ outbox failure update failed")
            return jsonify({"error": "outbox failure update failed"}), 503

    return app


class QQBridgeServer:
    """Thread-owned Werkzeug server used by :class:`email_bot.EmailBot`."""

    def __init__(
        self,
        handler: Any,
        *,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        server_factory: Callable[..., BaseWSGIServer] = make_server,
    ) -> None:
        self.host = host or os.getenv("QQ_BRIDGE_HOST", "127.0.0.1")
        self.port = int(port if port is not None else os.getenv("QQ_BRIDGE_PORT", "8082"))
        self.app = create_app(handler, token=token)
        self._server_factory = server_factory
        self._server: BaseWSGIServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if not self.app.config.get("QQ_BRIDGE_TOKEN"):
                raise RuntimeError("QQ_BRIDGE_TOKEN is required")
            self._server = self._server_factory(self.host, self.port, self.app, threaded=True)
            self._thread = threading.Thread(
                target=self._serve,
                name="qq-bridge-http",
                daemon=True,
            )
            self._thread.start()

    def _serve(self) -> None:
        try:
            assert self._server is not None
            self._server.serve_forever()
        except Exception:
            logger.exception("QQ bridge server stopped unexpectedly")

    def stop(self, timeout: float = 5) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                logger.exception("Could not stop QQ bridge server")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))


__all__ = ["QQBridgeServer", "create_app"]
