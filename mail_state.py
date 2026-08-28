"""Durable SQLite state for mail intake, work leases, and SMTP delivery.

The store deliberately has no knowledge of IMAP, media downloaders, or SMTP.
Callers can use it from synchronous worker threads; each public operation is a
short transaction and the connection is protected by a re-entrant lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit


SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_LEASE_SECONDS = 300.0
logger = logging.getLogger("MailStateStore")


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
        self._supports_partial_status = True

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
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                # The database remains usable on filesystems that do not
                # expose POSIX modes; Docker's state volume is still scoped.
                pass
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
            self._conn.execute("PRAGMA secure_delete = ON")
            secure_cleanup_required = False
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
                        intake_complete INTEGER NOT NULL DEFAULT 1
                            CHECK (intake_complete IN (0, 1)),
                        seen_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS source_locator_idx
                        ON source_messages(mailbox, uidvalidity, uid)
                        WHERE uid > 0;

                    CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_message_id TEXT NOT NULL
                            REFERENCES source_messages(source_message_id),
                        source_kind TEXT NOT NULL DEFAULT 'mail',
                        external_source_id TEXT,
                        normalized_url TEXT NOT NULL,
                        original_url TEXT NOT NULL,
                        platform TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'leased', 'succeeded',
                                              'partially_succeeded', 'failed')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        max_attempts INTEGER CHECK (max_attempts IS NULL OR max_attempts > 0),
                        lease_token TEXT,
                        lease_expires_at REAL,
                        next_attempt_at REAL,
                        last_error TEXT,
                        error_code TEXT,
                        retry_class TEXT,
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

                    CREATE TABLE IF NOT EXISTS task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL REFERENCES tasks(id),
                        event_type TEXT NOT NULL,
                        event_version INTEGER NOT NULL DEFAULT 1,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        UNIQUE (task_id, event_type)
                    );
                    CREATE INDEX IF NOT EXISTS task_events_pending_idx
                        ON task_events(id, created_at);

                    CREATE TABLE IF NOT EXISTS task_event_consumptions (
                        event_id INTEGER NOT NULL REFERENCES task_events(id),
                        consumer TEXT NOT NULL,
                        consumed_at REAL NOT NULL,
                        PRIMARY KEY (event_id, consumer)
                    );

                    CREATE TABLE IF NOT EXISTS mail_task_bindings (
                        task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                        source_message_id TEXT NOT NULL,
                        recipient TEXT NOT NULL DEFAULT '',
                        subject_context TEXT NOT NULL DEFAULT '{}',
                        UNIQUE (source_message_id, task_id)
                    );
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
                if "intake_complete" not in source_columns:
                    # Historical sources need conservative recovery. Only
                    # sources with a persisted route or a prior Seen ACK can
                    # be treated as complete.
                    self._conn.execute(
                        "ALTER TABLE source_messages ADD COLUMN intake_complete "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                task_columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(tasks)")
                }
                if "source_kind" not in task_columns:
                    self._conn.execute(
                        "ALTER TABLE tasks ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'mail'"
                    )
                if "external_source_id" not in task_columns:
                    self._conn.execute(
                        "ALTER TABLE tasks ADD COLUMN external_source_id TEXT"
                    )
                if "max_attempts" not in task_columns:
                    self._conn.execute("ALTER TABLE tasks ADD COLUMN max_attempts INTEGER")
                if "error_code" not in task_columns:
                    self._conn.execute("ALTER TABLE tasks ADD COLUMN error_code TEXT")
                if "retry_class" not in task_columns:
                    self._conn.execute("ALTER TABLE tasks ADD COLUMN retry_class TEXT")
                self._conn.execute(
                    "UPDATE tasks SET source_kind = COALESCE(NULLIF(source_kind, ''), 'mail'), "
                    "external_source_id = COALESCE(external_source_id, source_message_id)"
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_source_key_idx "
                    "ON tasks(source_kind, external_source_id, normalized_url) "
                    "WHERE external_source_id IS NOT NULL"
                )
                task_sql_row = self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
                ).fetchone()
                task_sql = str(task_sql_row[0] or "") if task_sql_row else ""
                self._supports_partial_status = (
                    "partially_succeeded" in task_sql
                    or "CHECK (status" not in task_sql
                )
                cookie_tasks_exist = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE platform = 'cookie' LIMIT 1"
                ).fetchone() is not None
                if version < SCHEMA_VERSION or cookie_tasks_exist:
                    # Also sanitize a v3 database left behind by an interrupted
                    # rollout; cookie tasks must never re-enter a worker.
                    self._migrate_legacy_state_locked()
                    if cookie_tasks_exist:
                        # A v3 database can still contain secret pages when a
                        # previous startup was interrupted after the logical
                        # redaction. Repeat the physical cleanup in that case.
                        secure_cleanup_required = True
                if version < SCHEMA_VERSION:
                    secure_cleanup_required = secure_cleanup_required or version < 2
            if secure_cleanup_required:
                # The legacy schema may have stored cookie plaintext in the
                # main DB or WAL. Checkpoint, securely rebuild, and truncate
                # before exposing the upgraded store to workers.
                self._secure_migrate_cleanup()
                # Only mark the migration complete after all physical cleanup
                # steps succeed. A failed cleanup must be retried next start.
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version < SCHEMA_VERSION:
                # No secret cleanup was needed, but the schema version still
                # has to advance after all transactional migrations succeed.
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

    def _migrate_legacy_state_locked(self) -> None:
        """Make pre-v2 sources and cookie tasks safe to resume."""
        self._conn.execute(
            "UPDATE source_messages SET intake_complete = CASE "
            "WHEN uid = 0 OR seen_at IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM tasks WHERE tasks.source_message_id = source_messages.source_message_id"
            ") THEN 1 ELSE 0 END"
        )
        rows = self._conn.execute(
            "SELECT id, source_message_id, payload_json, status "
            "FROM tasks WHERE platform = 'cookie'"
        ).fetchall()
        for row in rows:
            timestamp = self._now(None)
            try:
                payload = self._decode(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            sender = str(payload.get("sender", "") or "")
            source = self._conn.execute(
                "SELECT metadata_json FROM source_messages "
                "WHERE source_message_id = ?",
                (row["source_message_id"],),
            ).fetchone()
            if not sender and source is not None:
                try:
                    source_metadata = self._decode(source["metadata_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    source_metadata = {}
                if isinstance(source_metadata, dict):
                    sender = str(source_metadata.get("sender", "") or "")
            if not sender:
                outbox = self._conn.execute(
                    "SELECT payload_json FROM smtp_outbox WHERE task_id = ? "
                    "ORDER BY id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if outbox is not None:
                    try:
                        outbox_payload = self._decode(outbox["payload_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        outbox_payload = {}
                    if isinstance(outbox_payload, dict):
                        sender = str(outbox_payload.get("to_addr", "") or "")
            status = row["status"]
            if status in ("pending", "leased"):
                self._conn.execute(
                    "UPDATE tasks SET status = 'failed', payload_json = ?, "
                    "result_json = NULL, next_attempt_at = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, last_error = NULL, completed_at = COALESCE(completed_at, ?), "
                    "updated_at = ? WHERE id = ?",
                    (
                        self._json({}),
                        timestamp,
                        timestamp,
                        row["id"],
                    ),
                )
            else:
                self._conn.execute(
                    "UPDATE tasks SET payload_json = ?, result_json = NULL, "
                    "last_error = NULL, updated_at = ? WHERE id = ?",
                    (self._json({}), timestamp, row["id"]),
                )
            self._conn.execute(
                "UPDATE source_messages SET metadata_json = ? "
                "WHERE source_message_id = ?",
                (self._json({"sender": sender}), row["source_message_id"]),
            )
            safe_notice = self._json(
                {
                    "to_addr": sender,
                    "body": "旧版 Cookie 任务无法安全恢复，请通过 Web Login 或 uv run python get_cookie.py 更新 Cookie。",
                    "subject_status": "Cookie 需通过 Web Login 更新",
                }
            )
            self._conn.execute(
                "UPDATE smtp_outbox SET payload_json = ?, last_error = NULL, updated_at = ? "
                "WHERE task_id = ?",
                (safe_notice, timestamp, row["id"]),
            )
            if self._conn.execute(
                "SELECT 1 FROM smtp_outbox WHERE task_id = ? LIMIT 1", (row["id"],)
            ).fetchone() is None:
                self._insert_outbox_locked(
                    row["id"],
                    "legacy-cookie-recovery",
                    json.loads(safe_notice),
                    None,
                    timestamp,
                )

    def _secure_migrate_cleanup(self) -> None:
        """Physically remove pre-v2 secret pages before completing migration."""
        self._checkpoint_wal()
        self._conn.execute("VACUUM")
        self._checkpoint_wal()

    def _checkpoint_wal(self) -> None:
        result = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not result or int(result[0]) != 0:
            busy = result[0] if result else "unknown"
            raise RuntimeError(f"SQLite WAL checkpoint is busy: {busy}")

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
                "FROM source_messages WHERE mailbox = ? AND uid > 0 "
                "AND intake_complete = 1 AND seen_at IS NULL "
                "ORDER BY uidvalidity, uid",
                (mailbox,),
            ).fetchall()
            return [dict(row) for row in rows]

    def pending_intake(
        self, mailbox: str, uidvalidity: int
    ) -> list[dict[str, Any]]:
        """Return source UIDs whose routing transaction was interrupted."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT source_message_id, mailbox, uidvalidity, uid "
                "FROM source_messages WHERE mailbox = ? AND uidvalidity = ? "
                "AND uid > 0 AND intake_complete = 0 ORDER BY uid",
                (mailbox, uidvalidity),
            ).fetchall()
            return [dict(row) for row in rows]

    def unfinished_work_counts(self) -> dict[str, int]:
        """Return work that must drain before switching to the legacy bot."""
        with self._lock:
            self._ensure_open()
            intake_count = self._conn.execute(
                "SELECT COUNT(*) FROM source_messages "
                "WHERE uid > 0 AND (intake_complete = 0 OR seen_at IS NULL)"
            ).fetchone()[0]
            task_count = self._conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status IN ('pending', 'leased')"
            ).fetchone()[0]
            outbox_count = self._conn.execute(
                "SELECT COUNT(*) FROM smtp_outbox "
                "WHERE status IN ('pending', 'leased', 'failed')"
            ).fetchone()[0]
            return {
                "intake": int(intake_count),
                "tasks": int(task_count),
                "outbox": int(outbox_count),
            }

    def terminal_legacy_retry_keys(self, *, strict: bool = False) -> set[str]:
        """Return legacy JSON keys already terminal in durable state.

        Rollback uses ``strict=True`` because silently ignoring corrupt
        terminal payloads could allow the legacy path to duplicate or abandon
        work. Routine cleanup remains tolerant so one damaged record does not
        prevent unrelated terminal mirrors from being removed.
        """
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id, payload_json FROM tasks "
                "WHERE status IN ('succeeded', 'failed')"
            ).fetchall()
            keys: set[str] = set()
            for row in rows:
                try:
                    payload = self._decode(row["payload_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    if strict:
                        raise RuntimeError(
                            f"terminal task payload for task {row['id']} is invalid JSON"
                        ) from exc
                    logger.warning(
                        "Ignoring invalid terminal task payload for task %s",
                        row["id"],
                    )
                    continue
                if not isinstance(payload, dict):
                    if strict:
                        raise RuntimeError(
                            f"terminal task payload for task {row['id']} must be an object"
                        )
                    logger.warning(
                        "Ignoring non-object terminal task payload for task %s",
                        row["id"],
                    )
                    continue
                if "legacy_retry_key" not in payload:
                    continue
                key = payload["legacy_retry_key"]
                if not isinstance(key, str) or not key.strip():
                    if strict:
                        raise RuntimeError(
                            f"terminal task {row['id']} has an invalid legacy_retry_key"
                        )
                    logger.warning(
                        "Ignoring invalid legacy_retry_key in terminal task %s",
                        row["id"],
                    )
                    continue
                keys.add(key)
            return keys

    def mark_intake_complete(
        self, source_message_id: str, *, now: float | None = None
    ) -> bool:
        """Mark routing durable before allowing the source mail to be ACKed."""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE source_messages SET intake_complete = 1, updated_at = ? "
                "WHERE source_message_id = ?",
                (timestamp, source_message_id),
            )
            return cursor.rowcount == 1

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

    def replace_source_metadata(
        self,
        source_message_id: str,
        metadata: dict[str, Any],
        *,
        now: float | None = None,
    ) -> bool:
        """Replace source metadata, e.g. when quarantining a failed intake."""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE source_messages SET metadata_json = ?, updated_at = ? "
                "WHERE source_message_id = ?",
                (self._json(metadata), timestamp, source_message_id),
            )
            return cursor.rowcount == 1

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
        max_attempts: int | None = None,
        advance_position: bool = True,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Durably accept one source and create URL tasks idempotently.

        Mailbox sources start with ``intake_complete = 0``. The caller marks
        routing complete after any command or safe notice is persisted, then
        it may acknowledge the corresponding IMAP UID.
        """
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
            if advance_position:
                self._set_mailbox_position_locked(mailbox, uidvalidity, uid, timestamp)
            existing = self._conn.execute(
                "SELECT source_message_id FROM source_messages WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO source_messages "
                "(source_message_id, mailbox, uidvalidity, uid, metadata_json, "
                "intake_complete, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
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
                    source_kind="mail",
                    external_source_id=source_message_id,
                    max_attempts=max_attempts,
                )
                task_ids.append(task["id"])
                if task["created"]:
                    created_task_ids.append(task["id"])
                self._insert_mail_binding_locked(
                    task["id"], source_message_id, metadata or {}, timestamp
                )
            source = self._conn.execute(
                "SELECT intake_complete FROM source_messages "
                "WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
            assert source is not None
            return {
                "source_message_id": source_message_id,
                "duplicate": existing is not None,
                "intake_complete": bool(source["intake_complete"]),
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
        *,
        source_kind: str = "mail",
        external_source_id: str | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        cursor = self._conn.execute(
            "INSERT INTO tasks "
            "(source_message_id, source_kind, external_source_id, normalized_url, original_url, "
            "platform, payload_json, max_attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_message_id, normalized_url) DO NOTHING",
            (
                source_message_id,
                source_kind,
                external_source_id or source_message_id,
                normalized_url,
                original_url,
                platform,
                self._json(payload),
                max_attempts,
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

    def _insert_mail_binding_locked(
        self,
        task_id: int,
        source_message_id: str,
        metadata: Mapping[str, Any] | dict[str, Any],
        timestamp: float,
    ) -> None:
        sender = str(metadata.get("sender", "") or "")
        subject = str(metadata.get("subject", "") or "")
        self._conn.execute(
            "INSERT INTO mail_task_bindings "
            "(task_id, source_message_id, recipient, subject_context) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "recipient = CASE WHEN excluded.recipient <> '' THEN excluded.recipient ELSE recipient END, "
            "subject_context = CASE WHEN excluded.subject_context <> '{}' "
            "THEN excluded.subject_context ELSE subject_context END",
            (task_id, source_message_id, sender, self._json({"subject": subject, "updated_at": timestamp})),
        )

    def enqueue_task(
        self,
        source_message_id: str,
        url: str,
        *,
        original_url: str | None = None,
        payload: Any = None,
        platform: str | None = None,
        source_kind: str = "mail",
        external_source_id: str | None = None,
        max_attempts: int | None = None,
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
                "(source_message_id, mailbox, uidvalidity, uid, metadata_json, "
                "intake_complete, created_at, updated_at) "
                "VALUES (?, '', 0, 0, '{}', 1, ?, ?) "
                "ON CONFLICT(source_message_id) DO NOTHING",
                (source_message_id, timestamp, timestamp),
            )
            self._insert_task_locked(
                source_message_id,
                normalized,
                original_url if original_url is not None else url.strip(),
                payload,
                timestamp,
                platform,
                source_kind=source_kind,
                external_source_id=external_source_id or source_message_id,
                max_attempts=max_attempts,
            )
            row = self._conn.execute("SELECT * FROM tasks WHERE source_message_id = ? AND normalized_url = ?", (source_message_id, normalized)).fetchone()
            return self._row(row)  # type: ignore[return-value]

    def get_task(self, source_message_id: str, url: str) -> dict[str, Any] | None:
        """Return one idempotent task without changing its state."""
        normalized = self.normalize_url(url)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE source_message_id = ? "
                "AND normalized_url = ?",
                (source_message_id, normalized),
            ).fetchone()
            return self._row(row)

    def get_task_by_id(self, task_id: int) -> dict[str, Any] | None:
        """Return a task by its stable database id without changing state."""
        with self._lock:
            self._ensure_open()
            return self._row(
                self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )

    def set_task_result(
        self, task_id: int, result: Any, *, now: float | None = None
    ) -> bool:
        """Persist an intermediate/partial result without changing a lease."""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET result_json = ?, updated_at = ? WHERE id = ?",
                (self._json(result), timestamp, task_id),
            )
            return cursor.rowcount == 1

    def enqueue_notice(
        self,
        source_message_id: str,
        event: str,
        payload: Any,
        notification: Any,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically persist a safe notice task and its SMTP outbox item."""
        source_message_id = str(source_message_id).strip()
        event = str(event).strip()
        if not source_message_id:
            raise ValueError("source_message_id must not be empty")
        if not event:
            raise ValueError("event must not be empty")
        timestamp = self._now(now)
        normalized = self.normalize_url(f"urn:mail-notice:{event}")
        with self._lock, self._transaction():
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO source_messages "
                "(source_message_id, mailbox, uidvalidity, uid, metadata_json, "
                "intake_complete, created_at, updated_at) "
                "VALUES (?, '', 0, 0, '{}', 1, ?, ?) "
                "ON CONFLICT(source_message_id) DO NOTHING",
                (source_message_id, timestamp, timestamp),
            )
            task = self._insert_task_locked(
                source_message_id,
                normalized,
                f"urn:mail-notice:{event}",
                payload,
                timestamp,
                "notice",
            )
            self._conn.execute(
                "UPDATE tasks SET status = 'succeeded', result_json = ?, "
                "updated_at = ?, completed_at = COALESCE(completed_at, ?) "
                "WHERE id = ? AND status = 'pending'",
                (self._json({"notice": event}), timestamp, timestamp, task["id"]),
            )
            self._insert_outbox_locked(
                task["id"], event, notification, None, timestamp
            )
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()
            return self._row(row)  # type: ignore[return-value]

    def redact_task_payload(
        self,
        task_id: int,
        payload: Any,
        *,
        now: float | None = None,
    ) -> bool:
        """Replace a completed task payload, useful for removing secret input."""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE id = ?",
                (self._json(payload), timestamp, task_id),
            )
            return cursor.rowcount == 1

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
        status: str = "succeeded",
        error_code: str | None = None,
        retry_class: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if status not in ("succeeded", "partially_succeeded"):
            raise ValueError("complete_task status must be succeeded or partially_succeeded")
        physical_status = status
        if status == "partially_succeeded" and not self._supports_partial_status:
            # Pre-v3 CHECK constraints only know succeeded/failed.  The result
            # and immutable event still preserve the richer logical status.
            physical_status = "succeeded"
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET status = ?, lease_token = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL, last_error = NULL, error_code = ?, retry_class = ?, "
                "result_json = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (
                    physical_status,
                    error_code,
                    retry_class,
                    self._json(result),
                    timestamp,
                    timestamp,
                    task_id,
                    lease_token,
                    timestamp,
                ),
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
            self._insert_event_locked(
                task_id,
                f"task.{status}",
                {
                    "status": status,
                    "result": result,
                    "error_code": error_code,
                    "retry_class": retry_class,
                },
                timestamp,
            )
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def fail_task(
        self,
        task_id: int,
        lease_token: str,
        error: str | None = None,
        *,
        result: Any = None,
        retry_at: float | None = None,
        next_attempt_at: float | None = None,
        retry: bool = False,
        notification: Any = None,
        outbox_payload: Any = None,
        outbox_event: str = "failed",
        outbox_message_id: str | None = None,
        error_code: str | None = None,
        retry_class: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = self._now(now)
        scheduled = next_attempt_at if next_attempt_at is not None else retry_at
        if retry and scheduled is None:
            scheduled = timestamp
        status = "pending" if scheduled is not None else "failed"
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET status = ?, lease_token = NULL, lease_expires_at = NULL, "
                "next_attempt_at = ?, last_error = ?, error_code = ?, retry_class = ?, updated_at = ? "
                ", result_json = COALESCE(?, result_json) "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? AND lease_expires_at > ?",
                (
                    status,
                    scheduled,
                    error,
                    error_code,
                    retry_class,
                    timestamp,
                    self._json(result) if result is not None else None,
                    task_id,
                    lease_token,
                    timestamp,
                ),
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
            self._insert_event_locked(
                task_id,
                "task.retry_scheduled" if scheduled is not None else "task.failed",
                {
                    "status": status,
                    "error": error,
                    "error_code": error_code,
                    "retry_class": retry_class,
                    "next_attempt_at": scheduled,
                    "result": result,
                },
                timestamp,
            )
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def release_task(
        self,
        task_id: int,
        lease_token: str,
        *,
        next_attempt_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Return a claimed task to the queue without charging an attempt.

        Workers use this when a local capacity semaphore is busy. It is
        distinct from ``fail_task`` because no external work was attempted.
        """
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE tasks SET status = 'pending', attempts = MAX(attempts - 1, 0), "
                "lease_token = NULL, lease_expires_at = NULL, next_attempt_at = ?, "
                "updated_at = ? WHERE id = ? AND status = 'leased' AND lease_token = ?",
                (next_attempt_at, timestamp, task_id, lease_token),
            )
            if cursor.rowcount != 1:
                return None
            return self._row(self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

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

    def _insert_event_locked(
        self,
        task_id: int,
        event_type: str,
        payload: Any,
        timestamp: float,
        *,
        event_version: int = 1,
    ) -> dict[str, Any]:
        """Insert one immutable task event, returning the existing event on replay."""
        self._conn.execute(
            "INSERT INTO task_events "
            "(task_id, event_type, event_version, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, event_type) DO NOTHING",
            (task_id, event_type, event_version, self._json(payload), timestamp),
        )
        row = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? AND event_type = ?",
            (task_id, event_type),
        ).fetchone()
        if row is None:
            raise RuntimeError("task event insert did not produce a row")
        result = dict(row)
        result["payload"] = self._decode(result.pop("payload_json"))
        return result

    def record_task_event(
        self,
        task_id: int,
        event_type: str,
        payload: Any = None,
        *,
        event_version: int = 1,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Persist an idempotent event for an already stored task."""
        event_type = str(event_type).strip()
        if not event_type:
            raise ValueError("event_type must not be empty")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            if self._conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                raise ValueError(f"unknown task id: {task_id}")
            return self._insert_event_locked(
                task_id, event_type, payload, timestamp, event_version=event_version
            )

    def list_task_events(
        self,
        *,
        consumer: str | None = None,
        limit: int = 100,
        include_consumed: bool = False,
    ) -> list[dict[str, Any]]:
        """Read immutable events, optionally filtered by a consumer cursor."""
        if limit < 1:
            return []
        with self._lock:
            self._ensure_open()
            params: list[Any] = []
            query = "SELECT e.* FROM task_events e"
            if consumer is not None and not include_consumed:
                query += (
                    " LEFT JOIN task_event_consumptions c "
                    "ON c.event_id = e.id AND c.consumer = ?"
                )
                params.append(consumer)
            query += " WHERE 1=1"
            if consumer is not None and not include_consumed:
                query += " AND c.event_id IS NULL"
            query += " ORDER BY e.id LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = self._decode(item.pop("payload_json"))
                result.append(item)
            return result

    def consume_task_event(
        self, event_id: int, consumer: str, *, now: float | None = None
    ) -> bool:
        """Ack an event once for one consumer; duplicate acks are harmless."""
        consumer = str(consumer).strip()
        if not consumer:
            raise ValueError("consumer must not be empty")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "INSERT INTO task_event_consumptions(event_id, consumer, consumed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(event_id, consumer) DO NOTHING",
                (event_id, consumer, timestamp),
            )
            return cursor.rowcount == 1

    def project_task_event(
        self,
        event_id: int,
        consumer: str,
        *,
        outbox_event: str | None = None,
        outbox_payload: Any = None,
        outbox_message_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Atomically materialize an event into an outbox and acknowledge it.

        This closes the crash window between a durable task completion and an
        entry-specific notification.  Replaying the same event is safe due to
        both the consumption primary key and the outbox task/event uniqueness.
        """
        consumer = str(consumer).strip()
        if not consumer:
            raise ValueError("consumer must not be empty")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            event = self._conn.execute(
                "SELECT * FROM task_events WHERE id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise ValueError(f"unknown task event id: {event_id}")
            consumed = self._conn.execute(
                "SELECT 1 FROM task_event_consumptions WHERE event_id = ? AND consumer = ?",
                (event_id, consumer),
            ).fetchone()
            if consumed is not None:
                return False
            if outbox_event is not None:
                self._insert_outbox_locked(
                    int(event["task_id"]),
                    outbox_event,
                    outbox_payload,
                    outbox_message_id,
                    timestamp,
                )
            self._conn.execute(
                "INSERT INTO task_event_consumptions(event_id, consumer, consumed_at) "
                "VALUES (?, ?, ?)",
                (event_id, consumer, timestamp),
            )
            return True

    def unfinished_event_count(self, consumer: str | None = None) -> int:
        """Count events that a named projector has not durably consumed."""
        with self._lock:
            self._ensure_open()
            if consumer is None:
                return int(self._conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
            if consumer == "email":
                # Generic task events do not imply an email notification.  A
                # mail projector only owns tasks carrying an explicit sender,
                # which also preserves the legacy rollback facade for old
                # source-less tasks.
                rows = self._conn.execute(
                    "SELECT e.id, t.payload_json FROM task_events e "
                    "JOIN tasks t ON t.id = e.task_id "
                    "LEFT JOIN task_event_consumptions c "
                    "ON c.event_id = e.id AND c.consumer = ? "
                    "WHERE c.event_id IS NULL",
                    (consumer,),
                ).fetchall()
                count = 0
                for row in rows:
                    try:
                        payload = self._decode(row["payload_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                    if isinstance(payload, dict) and payload.get("sender"):
                        count += 1
                return count
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM task_events e "
                    "LEFT JOIN task_event_consumptions c ON c.event_id = e.id AND c.consumer = ? "
                    "WHERE c.event_id IS NULL",
                    (consumer,),
                ).fetchone()[0]
            )

    def task_has_outbox(self, task_id: int) -> bool:
        """Return whether an entry adapter already materialized a notice."""
        with self._lock:
            self._ensure_open()
            return (
                self._conn.execute(
                    "SELECT 1 FROM smtp_outbox WHERE task_id = ? LIMIT 1", (task_id,)
                ).fetchone()
                is not None
            )

    def heartbeat_outbox(
        self,
        outbox_id: int,
        lease_token: str,
        *,
        lease_seconds: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Extend an SMTP outbox lease while a provider call is in flight."""
        lease = self.default_lease_seconds if lease_seconds is None else float(lease_seconds)
        if lease <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            self._ensure_open()
            cursor = self._conn.execute(
                "UPDATE smtp_outbox SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_token = ? "
                "AND lease_expires_at > ?",
                (timestamp + lease, timestamp, outbox_id, lease_token, timestamp),
            )
            if cursor.rowcount != 1:
                return None
            return self._row(
                self._conn.execute("SELECT * FROM smtp_outbox WHERE id = ?", (outbox_id,)).fetchone()
            )

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
