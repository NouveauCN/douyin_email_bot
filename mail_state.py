"""Durable SQLite state for mail intake, work leases, and SMTP delivery.

The store deliberately has no knowledge of IMAP, media downloaders, or SMTP.
Callers can use it from synchronous worker threads; each public operation is a
short transaction and the connection is protected by a re-entrant lock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import SplitResult, urlsplit, urlunsplit


SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_LEASE_SECONDS = 300.0


class MailStateStore:
    """SQLite-backed state store with idempotent intake and expiring leases."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Any = time.time,
        default_lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        if default_lease_seconds <= 0:
            raise ValueError("default_lease_seconds must be positive")

        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.clock = clock
        self.default_lease_seconds = float(default_lease_seconds)
        self._lock = threading.RLock()
        self._closed = False

        db_path = Path(self.path)
        if self.path != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        """Create or validate the schema and configure durable SQLite behavior."""
        with self._lock:
            self._ensure_open()
            self._conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            # WAL is persistent database configuration; it is safe to set on
            # every open and makes readers independent from short write txns.
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA synchronous = FULL")
            with self._transaction():
                version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"unsupported mail state schema version {version}"
                    )
                schema = """
                    CREATE TABLE IF NOT EXISTS mailbox_positions (
                        mailbox TEXT PRIMARY KEY,
                        uidvalidity INTEGER NOT NULL,
                        last_uid INTEGER NOT NULL CHECK (last_uid >= 0),
                        generation INTEGER NOT NULL CHECK (generation >= 1),
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS mailbox_generations (
                        mailbox TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation >= 1),
                        uidvalidity INTEGER NOT NULL,
                        last_uid INTEGER NOT NULL CHECK (last_uid >= 0),
                        archived_at REAL NOT NULL,
                        PRIMARY KEY (mailbox, generation),
                        UNIQUE (mailbox, uidvalidity)
                    );

                    CREATE TABLE IF NOT EXISTS source_messages (
                        source_message_id TEXT PRIMARY KEY,
                        mailbox TEXT NOT NULL,
                        uidvalidity INTEGER NOT NULL,
                        uid INTEGER NOT NULL CHECK (uid >= 0),
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        seen_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_message_id TEXT NOT NULL
                            REFERENCES source_messages(source_message_id),
                        normalized_url TEXT NOT NULL,
                        original_url TEXT NOT NULL,
                        platform TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'leased', 'succeeded', 'failed')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
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
                    CREATE INDEX IF NOT EXISTS tasks_claim_idx
                        ON tasks(status, next_attempt_at, id);

                    CREATE TABLE IF NOT EXISTS smtp_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL REFERENCES tasks(id),
                        event TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'leased', 'sent', 'failed')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        next_attempt_at REAL,
                        lease_token TEXT,
                        lease_expires_at REAL,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        sent_at REAL,
                        UNIQUE (task_id, event)
                    );
                    CREATE INDEX IF NOT EXISTS outbox_claim_idx
                        ON smtp_outbox(status, next_attempt_at, id);
                    """
                statement = ""
                for line in schema.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._conn.execute(statement)
                        statement = ""
                if statement.strip():
                    self._conn.execute(statement)
                source_columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(source_messages)")
                }
                if "seen_at" not in source_columns:
                    self._conn.execute("ALTER TABLE source_messages ADD COLUMN seen_at REAL")
                if version < SCHEMA_VERSION:
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> MailStateStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("mail state store is closed")

    @staticmethod
    def normalize_url(url: str) -> str:
        """Return a stable URL key while retaining meaningful query values."""
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        value = url.strip()
        if not value:
            raise ValueError("url must not be empty")
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        hostname = parts.hostname
        if hostname is None:
            return value
        try:
            port = parts.port
        except ValueError:
            return value
        host = hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parts.username is not None:
            userinfo = parts.username
            if parts.password is not None:
                userinfo += f":{parts.password}"
            netloc = f"{userinfo}@{netloc}"
        default_port = (parts.scheme.lower() == "http" and port == 80) or (
            parts.scheme.lower() == "https" and port == 443
        )
        if port is not None and not default_port:
            netloc += f":{port}"
        normalized = SplitResult(
            parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""
        )
        return urlunsplit(normalized)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode(value: str | None) -> Any:
        if value is None:
            return None
        return json.loads(value)

    @staticmethod
    def _token() -> str:
        return uuid.uuid4().hex

    def _now(self, now: float | None) -> float:
        return float(self.clock() if now is None else now)

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("metadata_json", "payload_json", "result_json"):
            if key in result:
                decoded = self._decode(result.pop(key))
                result[key.removesuffix("_json")] = decoded
        return result

    def get_mailbox_position(self, mailbox: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT mailbox, uidvalidity, last_uid, generation, updated_at "
                "FROM mailbox_positions WHERE mailbox = ?",
                (mailbox,),
            ).fetchone()
            return self._row(row)

    def get_mailbox_generations(self, mailbox: str) -> list[dict[str, Any]]:
        """Return archived UID generations, useful when reconciling a reset."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT mailbox, generation, uidvalidity, last_uid, archived_at "
                "FROM mailbox_generations WHERE mailbox = ? ORDER BY generation",
                (mailbox,),
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def pending_seen(self, mailbox: str) -> list[dict[str, Any]]:
        """Return accepted messages whose IMAP ``\\Seen`` ACK is pending."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT source_message_id, mailbox, uidvalidity, uid "
                "FROM source_messages WHERE mailbox = ? AND seen_at IS NULL "
                "ORDER BY uidvalidity, uid",
                (mailbox,),
            ).fetchall()
            return [dict(row) for row in rows]

    def ack_message(self, source_message_id: str, *, now: float | None = None) -> bool:
        """Record a successful IMAP ``\\Seen`` operation."""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            self._conn.execute(
                "UPDATE source_messages SET seen_at = ?, updated_at = ? "
                "WHERE source_message_id = ?",
                (timestamp, timestamp, source_message_id),
            )
            return self._conn.execute("SELECT changes()").fetchone()[0] == 1

    def set_mailbox_position(
        self, mailbox: str, uidvalidity: int, last_uid: int, *, now: float | None = None
    ) -> dict[str, Any]:
        """Advance a mailbox position, archiving the old generation if needed."""
        if last_uid < 0:
            raise ValueError("last_uid must be non-negative")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            return self._set_mailbox_position_locked(mailbox, uidvalidity, last_uid, timestamp)

    def _set_mailbox_position_locked(
        self, mailbox: str, uidvalidity: int, last_uid: int, timestamp: float
    ) -> dict[str, Any]:
        current = self._conn.execute(
            "SELECT * FROM mailbox_positions WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        if current is None:
            generation = 1
            self._conn.execute(
                "INSERT INTO mailbox_positions VALUES (?, ?, ?, ?, ?)",
                (mailbox, uidvalidity, last_uid, generation, timestamp),
            )
        elif current["uidvalidity"] != uidvalidity:
            self._conn.execute(
                "INSERT OR IGNORE INTO mailbox_generations "
                "(mailbox, generation, uidvalidity, last_uid, archived_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    mailbox,
                    current["generation"],
                    current["uidvalidity"],
                    current["last_uid"],
                    timestamp,
                ),
            )
            generation = current["generation"] + 1
            self._conn.execute(
                "UPDATE mailbox_positions SET uidvalidity = ?, last_uid = ?, "
                "generation = ?, updated_at = ? WHERE mailbox = ?",
                (uidvalidity, last_uid, generation, timestamp, mailbox),
            )
        else:
            generation = current["generation"]
            last_uid = max(current["last_uid"], last_uid)
            self._conn.execute(
                "UPDATE mailbox_positions SET last_uid = ?, updated_at = ? "
                "WHERE mailbox = ?",
                (last_uid, timestamp, mailbox),
            )
        return self.get_mailbox_position(mailbox)  # type: ignore[return-value]

    def accept_message(
        self,
        mailbox: str,
        uidvalidity: int,
        uid: int,
        source_message_id: str,
        urls: list[str] | tuple[str, ...] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        platform: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Durably accept one source message and create URL tasks idempotently."""
        if uid < 0:
            raise ValueError("uid must be non-negative")
        source_message_id = str(source_message_id).strip()
        if not source_message_id:
            raise ValueError("source_message_id must not be empty")
        timestamp = self._now(now)
        unique_urls: list[tuple[str, str]] = []
        seen: set[str] = set()
        for url in urls or ():
            normalized = self.normalize_url(url)
            if normalized not in seen:
                unique_urls.append((normalized, url.strip()))
                seen.add(normalized)

        with self._lock, self._transaction():
            self._ensure_open()
            self._set_mailbox_position_locked(mailbox, uidvalidity, uid, timestamp)
            existing = self._conn.execute(
                "SELECT source_message_id FROM source_messages WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO source_messages "
                "(source_message_id, mailbox, uidvalidity, uid, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_message_id) DO UPDATE SET updated_at = excluded.updated_at",
                (
                    source_message_id,
                    mailbox,
                    uidvalidity,
                    uid,
                    self._json(metadata),
                    timestamp,
                    timestamp,
                ),
            )
            task_ids: list[int] = []
            created_task_ids: list[int] = []
            for normalized, original in unique_urls:
                task = self._insert_task_locked(
                    source_message_id,
                    normalized,
                    original,
                    metadata,
                    timestamp,
                    platform,
                )
                task_ids.append(task["id"])
                if task["created"]:
                    created_task_ids.append(task["id"])
            return {
                "source_message_id": source_message_id,
                "duplicate": existing is not None,
                "task_ids": task_ids,
                "created_task_ids": created_task_ids,
                "position": self.get_mailbox_position(mailbox),
            }

    def _insert_task_locked(
        self,
        source_message_id: str,
        normalized_url: str,
        original_url: str,
        payload: Any,
        timestamp: float,
        platform: str | None = None,
    ) -> dict[str, Any]:
        cursor = self._conn.execute(
            "INSERT INTO tasks "
            "(source_message_id, normalized_url, original_url, platform, payload_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_message_id, normalized_url) DO NOTHING",
            (
                source_message_id,
                normalized_url,
                original_url,
                platform,
                self._json(payload),
                timestamp,
                timestamp,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM tasks WHERE source_message_id = ? AND normalized_url = ?",
            (source_message_id, normalized_url),
        ).fetchone()
        assert row is not None
        return {"id": row["id"], "created": cursor.rowcount == 1}

    def enqueue_task(
        self,
        source_message_id: str,
        url: str,
        *,
        original_url: str | None = None,
        payload: Any = None,
        platform: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Insert or return a task keyed by source message and normalized URL."""
        source_message_id = str(source_message_id).strip()
        if not source_message_id:
            raise ValueError("source_message_id must not be empty")
        normalized = self.normalize_url(url)
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO source_messages "
                "(source_message_id, mailbox, uidvalidity, uid, metadata_json, created_at, updated_at) "
                "VALUES (?, '', 0, 0, '{}', ?, ?) ON CONFLICT(source_message_id) DO NOTHING",
                (source_message_id, timestamp, timestamp),
            )
            self._insert_task_locked(
                source_message_id,
                normalized,
                original_url if original_url is not None else url.strip(),
                payload,
                timestamp,
                platform,
            )
            row = self._conn.execute("SELECT * FROM tasks WHERE source_message_id = ? AND normalized_url = ?", (source_message_id, normalized)).fetchone()
            return self._row(row)  # type: ignore[return-value]

    def claim_tasks(
        self,
        limit: int = 1,
        *,
        lease_seconds: float | None = None,
        worker_id: str | None = None,
        platform: str | None = None,
        filter: Any = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        lease = self.default_lease_seconds if lease_seconds is None else float(lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            candidate_limit = limit if filter is None else -1
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE status = 'pending' AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= ?) "
                + ("AND platform = ? " if platform is not None else "")
                + "ORDER BY id LIMIT ?",
                ((timestamp, platform, candidate_limit) if platform is not None else (timestamp, candidate_limit)),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                if len(claimed) >= limit:
                    break
                candidate = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
                candidate_dict = self._row(candidate)
                assert candidate_dict is not None
                if not self._matches_filter(candidate_dict, filter):
                    continue
                token = self._token()
                self._conn.execute(
                    "UPDATE tasks SET status = 'leased', attempts = attempts + 1, "
                    "lease_token = ?, lease_expires_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (token, timestamp + lease, timestamp, row["id"]),
                )
                item = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
                result = self._row(item)
                assert result is not None
                result["worker_id"] = worker_id
                claimed.append(result)
            return claimed

    @staticmethod
    def _matches_filter(row: dict[str, Any], filter: Any) -> bool:
        if filter is None:
            return True
        if callable(filter):
            return bool(filter(row))
        if isinstance(filter, dict):
            return all(row.get(key) == value for key, value in filter.items())
        raise TypeError("filter must be callable or a mapping")

    def heartbeat_task(
        self,
        task_id: int,
        lease_token: str,
        *,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        lease = self.default_lease_seconds if lease_seconds is None else float(lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            self._conn.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? "
                "AND lease_expires_at > ?",
                (timestamp + lease, timestamp, task_id, lease_token, timestamp),
            )
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ? AND status = 'leased' AND lease_token = ?", (task_id, lease_token)).fetchone())

    def complete_task(
        self,
        task_id: int,
        lease_token: str,
        *,
        result: Any = None,
        notification: Any = None,
        outbox_payload: Any = None,
        outbox_event: str = "completed",
        outbox_message_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET status = 'succeeded', lease_token = NULL, lease_expires_at = NULL, "
                "result_json = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (self._json(result), timestamp, timestamp, task_id, lease_token, timestamp),
            )
            if cursor.rowcount != 1:
                return None
            if notification is not None or outbox_payload is not None:
                self._insert_outbox_locked(
                    task_id,
                    outbox_event,
                    outbox_payload if outbox_payload is not None else notification,
                    outbox_message_id,
                    timestamp,
                )
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def fail_task(
        self,
        task_id: int,
        lease_token: str,
        error: str | None = None,
        *,
        retry_at: float | None = None,
        next_attempt_at: float | None = None,
        retry: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = self._now(now)
        scheduled = next_attempt_at if next_attempt_at is not None else retry_at
        if retry and scheduled is None:
            scheduled = timestamp
        status = "pending" if scheduled is not None else "failed"
        with self._lock, self._transaction():
            self._ensure_open()
            self._conn.execute(
                "UPDATE tasks SET status = ?, lease_token = NULL, lease_expires_at = NULL, "
                "next_attempt_at = ?, last_error = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (status, scheduled, error, timestamp, task_id, lease_token, timestamp),
            )
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()) if self._conn.execute("SELECT changes()").fetchone()[0] else None

    def recover_expired(self, *, now: float | None = None) -> dict[str, int]:
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            task_count = self._conn.execute(
                "UPDATE tasks SET status = 'pending', lease_token = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL, updated_at = ? WHERE status = 'leased' AND lease_expires_at <= ?",
                (timestamp, timestamp),
            ).rowcount
            outbox_count = self._conn.execute(
                "UPDATE smtp_outbox SET status = 'pending', lease_token = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE status = 'leased' AND lease_expires_at <= ?",
                (timestamp, timestamp),
            ).rowcount
            return {"tasks": task_count, "outbox": outbox_count}

    def enqueue_outbox(
        self,
        task_id: int,
        event: str,
        payload: Any = None,
        *,
        message_id: str | None = None,
        next_attempt_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        event = str(event).strip()
        if not event:
            raise ValueError("event must not be empty")
        timestamp = self._now(now)
        generated_message_id = message_id or self._stable_message_id(task_id, event)
        with self._lock, self._transaction():
            self._ensure_open()
            return self._insert_outbox_locked(
                task_id,
                event,
                payload,
                generated_message_id,
                timestamp,
                next_attempt_at=next_attempt_at,
            )

    def _insert_outbox_locked(
        self,
        task_id: int,
        event: str,
        payload: Any,
        message_id: str | None,
        timestamp: float,
        *,
        next_attempt_at: float | None = None,
    ) -> dict[str, Any]:
        generated_message_id = message_id or self._stable_message_id(task_id, event)
        self._conn.execute(
            "INSERT INTO smtp_outbox "
            "(task_id, event, message_id, payload_json, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, event) DO NOTHING",
            (task_id, event, generated_message_id, self._json(payload), next_attempt_at, timestamp, timestamp),
        )
        row = self._conn.execute("SELECT * FROM smtp_outbox WHERE task_id = ? AND event = ?", (task_id, event)).fetchone()
        if row is None:
            raise RuntimeError("outbox insert did not produce a row")
        return self._row(row)  # type: ignore[return-value]

    @staticmethod
    def _stable_message_id(task_id: int, event: str) -> str:
        digest = hashlib.sha256(event.encode("utf-8")).hexdigest()[:20]
        return f"<mail-state-{task_id}-{digest}@localhost>"

    def claim_outbox(
        self,
        limit: int = 1,
        *,
        lease_seconds: float | None = None,
        worker_id: str | None = None,
        platform: str | None = None,
        filter: Any = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        lease = self.default_lease_seconds if lease_seconds is None else float(lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            candidate_limit = limit if filter is None else -1
            rows = self._conn.execute(
                "SELECT o.id FROM smtp_outbox o JOIN tasks t ON t.id = o.task_id WHERE "
                "(o.status = 'pending' OR (o.status = 'failed' AND o.next_attempt_at IS NOT NULL AND o.next_attempt_at <= ?)) "
                + ("AND t.platform = ? " if platform is not None else "")
                + "ORDER BY o.id LIMIT ?",
                ((timestamp, platform, candidate_limit) if platform is not None else (timestamp, candidate_limit)),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                if len(claimed) >= limit:
                    break
                candidate = self._conn.execute(
                    "SELECT o.*, t.platform AS platform FROM smtp_outbox o JOIN tasks t ON t.id = o.task_id WHERE o.id = ?",
                    (row["id"],),
                ).fetchone()
                candidate_dict = self._row(candidate)
                assert candidate_dict is not None
                if not self._matches_filter(candidate_dict, filter):
                    continue
                token = self._token()
                self._conn.execute(
                    "UPDATE smtp_outbox SET status = 'leased', attempts = attempts + 1, "
                    "lease_token = ?, lease_expires_at = ?, updated_at = ?, next_attempt_at = NULL "
                    "WHERE id = ? AND (status = 'pending' OR status = 'failed')",
                    (token, timestamp + lease, timestamp, row["id"]),
                )
                item = self._conn.execute(
                    "SELECT o.*, t.platform AS platform FROM smtp_outbox o JOIN tasks t ON t.id = o.task_id WHERE o.id = ?",
                    (row["id"],),
                ).fetchone()
                result = self._row(item)
                assert result is not None
                result["worker_id"] = worker_id
                claimed.append(result)
            return claimed

    def mark_outbox_sent(
        self, outbox_id: int, lease_token: str, *, sent_at: float | None = None, now: float | None = None
    ) -> dict[str, Any] | None:
        timestamp = self._now(now)
        sent_timestamp = timestamp if sent_at is None else sent_at
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE smtp_outbox SET status = 'sent', lease_token = NULL, lease_expires_at = NULL, "
                "last_error = NULL, sent_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (sent_timestamp, timestamp, outbox_id, lease_token, timestamp),
            )
            if cursor.rowcount != 1:
                return None
            return self._row(self._conn.execute("SELECT * FROM smtp_outbox WHERE id = ?", (outbox_id,)).fetchone())

    def mark_outbox_failed(
        self,
        outbox_id: int,
        lease_token: str,
        error: str | None = None,
        *,
        retry_at: float | None = None,
        next_attempt_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = self._now(now)
        scheduled = next_attempt_at if next_attempt_at is not None else retry_at
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE smtp_outbox SET status = 'failed', lease_token = NULL, lease_expires_at = NULL, "
                "last_error = ?, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (error, scheduled, timestamp, outbox_id, lease_token, timestamp),
            )
            return self._row(self._conn.execute("SELECT * FROM smtp_outbox WHERE id = ?", (outbox_id,)).fetchone()) if cursor.rowcount == 1 else None


__all__ = ["MailStateStore", "SCHEMA_VERSION"]
