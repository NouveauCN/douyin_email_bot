"""Persistent settings shared by the browser and the bot.

The store deliberately contains no application-specific imports.  It is a
small, transactional SQLite registry which can be used by Flask handlers and
by the bot at startup.  Values are JSON encoded so lists (notably the sender
allow-list) retain their type.  Secrets are never returned by :func:`snapshot`
unless the caller explicitly asks for the value (the web API should not do
that).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    env: str | None
    value_type: str
    secret: bool = False
    editable: bool = True
    apply_mode: str = "restart"


def _defs() -> tuple[SettingDefinition, ...]:
    """The single explicit registry of settings exposed to the UI."""
    rows: list[tuple[str, str | None, str, bool, bool, str]] = [
        ("email.imap_server", "EMAIL_IMAP_SERVER", "str", False, True, "restart"),
        ("email.imap_port", "EMAIL_IMAP_PORT", "int", False, True, "restart"),
        ("email.smtp_server", "EMAIL_SMTP_SERVER", "str", False, True, "restart"),
        ("email.smtp_port", "EMAIL_SMTP_PORT", "int", False, True, "restart"),
        ("email.email", "EMAIL_ADDRESS", "str", True, True, "restart"),
        ("email.password", "EMAIL_PASSWORD", "str", True, True, "restart"),
        ("email.poll_interval", "EMAIL_POLL_INTERVAL", "int", False, True, "restart"),
        ("email.smtp_timeout", "SMTP_TIMEOUT", "int", False, True, "restart"),
        ("douyin.download_path", "DOUYIN_DOWNLOAD_PATH", "path", False, False, "restart"),
        ("douyin.cookie", "DOUYIN_COOKIE", "str", True, True, "hot"),
        ("douyin.naming", "DOUYIN_NAMING", "str", False, True, "restart"),
        ("douyin.folderize", "DOUYIN_FOLDERIZE", "bool", False, True, "restart"),
        ("douyin.timeout", "DOUYIN_TIMEOUT", "int", False, True, "restart"),
        ("douyin.max_retries", "DOUYIN_MAX_RETRIES", "int", False, True, "restart"),
        ("douyin.max_tasks", "DOUYIN_MAX_TASKS", "int", False, True, "restart"),
        ("bilibili.download_path", "BILIBILI_DOWNLOAD_PATH", "path", False, False, "restart"),
        ("bilibili.auth", "BILIBILI_AUTH", "str", True, True, "restart"),
        ("bilibili.auth_file", "BILIBILI_AUTH_FILE", "path", True, False, "restart"),
        ("bilibili.timeout", "BILIBILI_TIMEOUT", "int", False, True, "restart"),
        ("bilibili.batch", "BILIBILI_BATCH", "bool", False, True, "restart"),
        ("bilibili.video_quality", "BILIBILI_VIDEO_QUALITY", "int", False, True, "restart"),
        ("bilibili.yutto_bin", "BILIBILI_YUTTO_BIN", "path", False, False, "restart"),
        ("bot.allowed_senders", "BOT_ALLOWED_SENDERS", "str_list", False, True, "restart"),
        ("bot.cooldown_seconds", "BOT_COOLDOWN_SECONDS", "int", False, True, "restart"),
        ("bot.subject_keyword", "BOT_SUBJECT_KEYWORD", "str", False, True, "restart"),
        ("bot.transient_retry_attempts", "BOT_TRANSIENT_RETRY_ATTEMPTS", "int", False, True, "restart"),
        ("bot.transient_retry_delay_seconds", "BOT_TRANSIENT_RETRY_DELAY_SECONDS", "int", False, True, "restart"),
        ("bot.transient_pending_file", "BOT_TRANSIENT_PENDING_FILE", "path", False, False, "restart"),
        ("bot.transient_failed_file", "BOT_TRANSIENT_FAILED_FILE", "path", False, False, "restart"),
        ("bot.durable_mail_enabled", "BOT_DURABLE_MAIL_ENABLED", "bool", False, False, "restart"),
        ("bot.state_db", "BOT_STATE_DB", "path", False, False, "restart"),
        ("bot.worker_count", "BOT_WORKER_COUNT", "int", False, True, "restart"),
        ("bot.douyin_worker_count", "BOT_DOUYIN_WORKER_COUNT", "int", False, True, "restart"),
        ("bot.bilibili_worker_count", "BOT_BILIBILI_WORKER_COUNT", "int", False, True, "restart"),
        ("bot.lease_seconds", "BOT_LEASE_SECONDS", "int", False, True, "restart"),
        ("bot.heartbeat_seconds", "BOT_HEARTBEAT_SECONDS", "int", False, True, "restart"),
        ("bot.outbox_retry_attempts", "BOT_OUTBOX_RETRY_ATTEMPTS", "int", False, True, "restart"),
        ("bot.outbox_retry_delay_seconds", "BOT_OUTBOX_RETRY_DELAY_SECONDS", "int", False, True, "restart"),
        ("bot.commands.cookie_update", "BOT_COOKIE_UPDATE_COMMAND", "str", False, True, "restart"),
        ("bot.commands.cookie_auto", "BOT_COOKIE_AUTO_COMMAND", "str", False, True, "restart"),
        ("media_cleanup.backup_retention_days", "MEDIA_BACKUP_RETENTION_DAYS", "int", False, True, "restart"),
        ("media_cleanup.check_interval_days", "MEDIA_BACKUP_CHECK_INTERVAL_DAYS", "int", False, True, "restart"),
        ("cookie_extractor.profile_dir", "COOKIE_PROFILE_DIR", "path", False, False, "restart"),
        ("cookie_extractor.headless", "COOKIE_HEADLESS", "bool", False, True, "restart"),
        ("cookie_extractor.validate", "COOKIE_VALIDATE", "bool", False, True, "restart"),
    ]
    return tuple(SettingDefinition(*row) for row in rows)


SETTING_REGISTRY: dict[str, SettingDefinition] = {item.key: item for item in _defs()}
ENV_TO_KEY = {item.env: item.key for item in SETTING_REGISTRY.values() if item.env}

DEFAULT_VALUES: dict[str, Any] = {
    "email.imap_server": "imap.qq.com", "email.imap_port": 993,
    "email.smtp_server": "smtp.qq.com", "email.smtp_port": 587,
    "email.email": "", "email.password": "", "email.poll_interval": 30,
    "email.smtp_timeout": 30, "douyin.download_path": "./downloads",
    "douyin.cookie": "", "douyin.naming": "{create}_{aweme_id}",
    "douyin.folderize": True, "douyin.timeout": 30, "douyin.max_retries": 3,
    "douyin.max_tasks": 5, "bilibili.download_path": "./downloads/bilibili",
    "bilibili.auth": "", "bilibili.auth_file": "", "bilibili.timeout": 3600,
    "bilibili.batch": False, "bilibili.video_quality": 127,
    "bilibili.yutto_bin": "yutto", "bot.allowed_senders": [],
    "bot.cooldown_seconds": 5, "bot.subject_keyword": "下载",
    "bot.transient_retry_attempts": 3, "bot.transient_retry_delay_seconds": 120,
    "bot.transient_pending_file": "./pending_retries.json",
    "bot.transient_failed_file": "./failed_links.txt", "bot.durable_mail_enabled": True,
    "bot.state_db": "./state/mail_state.sqlite3", "bot.worker_count": 2,
    "bot.douyin_worker_count": 1, "bot.bilibili_worker_count": 1,
    "bot.lease_seconds": 300, "bot.heartbeat_seconds": 30,
    "bot.outbox_retry_attempts": 5, "bot.outbox_retry_delay_seconds": 60,
    "bot.commands.cookie_update": "更新cookie", "bot.commands.cookie_auto": "自动获取cookie",
    "media_cleanup.backup_retention_days": 28, "media_cleanup.check_interval_days": 7,
    "cookie_extractor.profile_dir": "", "cookie_extractor.headless": True,
    "cookie_extractor.validate": True,
}


def setting_registry() -> dict[str, SettingDefinition]:
    return dict(SETTING_REGISTRY)


def default_database_path(config_path: str | Path | None = None) -> Path:
    override = os.getenv("RUNTIME_SETTINGS_DB")
    if override:
        return Path(override).expanduser()
    base = Path(config_path).expanduser().resolve().parent if config_path else Path.cwd()
    return base / ".runtime-settings" / "settings.sqlite3"


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def read_dotenv(path: str | Path) -> dict[str, str]:
    """Read a dotenv file without modifying ``os.environ``."""
    result: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.is_file():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(line)
        if not match or match.group(1).startswith("#"):
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result[match.group(1)] = value
    return result


def _coerce(value: Any, value_type: str) -> Any:
    if value_type in ("str", "path"):
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value
    if value_type == "str_list":
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list) and all(isinstance(part, str) for part in value):
            return [part.strip() for part in value if part.strip()]
        raise ValueError("must be a list of strings")
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            return value.strip().lower() in {"1", "true", "yes", "on"}
        raise ValueError("must be a boolean")
    if value_type == "int":
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be an integer") from exc
    raise ValueError(f"unknown setting type: {value_type}")


def _reject_control_characters(value: Any) -> None:
    if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("control characters are not allowed")
    if isinstance(value, list):
        for item in value:
            _reject_control_characters(item)


def _validate_value(key: str, value: Any) -> Any:
    definition = SETTING_REGISTRY.get(key)
    if definition is None:
        raise ValueError(f"unknown setting: {key}")
    _reject_control_characters(value)
    value = _coerce(value, definition.value_type)
    if definition.value_type in {"str", "path"}:
        limit = 1_048_576 if definition.secret else 4096
        if len(value) > limit:
            raise ValueError("value is too long")
    if definition.value_type == "str_list":
        if len(value) > 200:
            raise ValueError("allow-list cannot contain more than 200 items")
        if any(len(item) > 320 for item in value):
            raise ValueError("allow-list item is too long")
        if sum(len(item) for item in value) > 65536:
            raise ValueError("allow-list is too large")
    if definition.value_type == "int":
        if key in {"email.imap_port", "email.smtp_port"} and not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if value < 1 and key not in {"bot.cooldown_seconds"}:
            raise ValueError("must be positive")
        if key == "bot.cooldown_seconds" and value < 0:
            raise ValueError("must not be negative")
        upper_bounds = {
            "email.poll_interval": 86400, "email.smtp_timeout": 86400,
            "douyin.timeout": 86400, "douyin.max_retries": 100,
            "douyin.max_tasks": 100, "bilibili.timeout": 86400,
            "bilibili.video_quality": 1000, "bot.cooldown_seconds": 86400,
            "bot.transient_retry_attempts": 100,
            "bot.transient_retry_delay_seconds": 86400,
            "bot.worker_count": 100, "bot.douyin_worker_count": 100,
            "bot.bilibili_worker_count": 100, "bot.lease_seconds": 86400,
            "bot.heartbeat_seconds": 86400, "bot.outbox_retry_attempts": 100,
            "bot.outbox_retry_delay_seconds": 86400,
            "media_cleanup.backup_retention_days": 36500,
            "media_cleanup.check_interval_days": 36500,
        }
        if key in upper_bounds and value > upper_bounds[key]:
            raise ValueError("value is unreasonably large")
    if key == "email.email" and value and ("@" not in value or any(ch.isspace() for ch in value)):
        raise ValueError("must be a valid email address")
    return value


class SettingsStore:
    """SQLite managed settings and bot lifecycle metadata."""

    def __init__(self, db_path: str | Path | None = None, *, config_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else default_database_path(config_path)
        self.path = self.path.expanduser()
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        self.path.touch(exist_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata(key, value) VALUES ('revision', '0');
                CREATE TABLE IF NOT EXISTS restart_requests (
                    request_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, error TEXT,
                    active_count INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS heartbeat (
                    component TEXT PRIMARY KEY, boot_id TEXT, revision INTEGER,
                    seen_at REAL NOT NULL, status TEXT, detail TEXT
                );
                """
            )
            # Keep databases created by the first settings-store version
            # usable when the lifecycle detail columns are introduced.
            columns = {row[1] for row in db.execute("PRAGMA table_info(restart_requests)")}
            if "active_count" not in columns:
                db.execute("ALTER TABLE restart_requests ADD COLUMN active_count INTEGER NOT NULL DEFAULT 0")
            if "detail" not in columns:
                db.execute("ALTER TABLE restart_requests ADD COLUMN detail TEXT NOT NULL DEFAULT ''")

    def get_revision(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])

    def managed_values(self, *, include_secrets: bool = True) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key, value FROM settings").fetchall()
        return self._decode_managed_rows(rows, include_secrets=include_secrets)

    @staticmethod
    def _decode_managed_rows(rows, *, include_secrets: bool = True) -> dict[str, Any]:
        result = {}
        for row in rows:
            if include_secrets or not SETTING_REGISTRY.get(row["key"], SettingDefinition("", None, "str")).secret:
                result[row["key"]] = json.loads(row["value"])
        return result

    def _managed_values_db(self, db: sqlite3.Connection) -> dict[str, Any]:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        return self._decode_managed_rows(rows)

    def get(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def _yaml_and_legacy(self, config_path: str | Path | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Read lower-priority files without importing the config loader."""
        path = Path(config_path).expanduser() if config_path else self.config_path
        if path is None:
            return {}, {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        yaml_values: dict[str, Any] = {}
        for key in SETTING_REGISTRY:
            section, field = key.split(".", 1)
            current: Any = raw.get(section, {})
            for part in field.split("."):
                current = current.get(part) if isinstance(current, Mapping) else None
            if current is not None:
                yaml_values[key] = current
        return yaml_values, read_dotenv(path.parent / ".env")

    def _candidate_effective_values(
        self,
        candidate_managed: Mapping[str, Any],
        *,
        config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        yaml_values, legacy_env = self._yaml_and_legacy(config_path)
        effective: dict[str, Any] = {}
        for key, definition in SETTING_REGISTRY.items():
            if definition.env and os.getenv(definition.env) is not None:
                value = os.getenv(definition.env)
            elif key in candidate_managed:
                value = candidate_managed[key]
            elif definition.env and definition.env in legacy_env:
                value = legacy_env[definition.env]
            else:
                value = yaml_values.get(key, DEFAULT_VALUES.get(key))
            try:
                effective[key] = _coerce(value, definition.value_type)
            except ValueError:
                effective[key] = value
        return effective

    def _validate_candidate(
        self,
        touched_keys: list[str],
        candidate_sets: Mapping[str, Any],
        resets: list[str],
        *,
        config_path: str | Path | None = None,
        managed: Mapping[str, Any] | None = None,
    ) -> None:
        candidate = dict(self.managed_values() if managed is None else managed)
        candidate.update(candidate_sets)
        for key in resets:
            candidate.pop(key, None)
        effective = self._candidate_effective_values(candidate, config_path=config_path)
        touched = set(touched_keys)
        if any(key.startswith("email.") for key in touched):
            if not str(effective.get("email.email", "")).strip() or not str(effective.get("email.password", "")).strip():
                raise ValueError("email address and password must both be configured")
        required_strings = {
            "email.imap_server", "email.smtp_server", "bot.subject_keyword",
            "douyin.naming", "bot.commands.cookie_update", "bot.commands.cookie_auto",
        }
        for key in touched & required_strings:
            if not str(effective.get(key, "")).strip():
                raise ValueError(f"{key} must not be empty")

    def apply_changes(self, changes: Mapping[str, Any], base_revision: int | None = None) -> int:
        """Atomically validate and set managed values, returning new revision."""
        return self.apply_and_maybe_restart(
            [{"key": key, "action": "set", "value": value} for key, value in changes.items()],
            base_revision=base_revision,
            request_restart=False,
        )["revision"]

    def apply(self, changes: list[Mapping[str, Any]], base_revision: int | None = None) -> int:
        """Apply browser-style ``[{key, action, value?}]`` changes.

        ``set`` stores a value, ``clear`` stores an explicit empty value (used
        for secrets), and ``reset`` removes the managed override.  All
        operations in one call share one optimistic revision and transaction.
        """
        return self.apply_and_maybe_restart(changes, base_revision=base_revision, request_restart=False)["revision"]

    def apply_and_maybe_restart(
        self,
        changes: list[Mapping[str, Any]],
        base_revision: int | None = None,
        *,
        request_restart: bool = True,
    ) -> dict[str, Any]:
        """Apply changes and optionally enqueue exactly one restart atomically.

        The return object is ``revision``, ``request_id`` (or ``None`` for a
        hot-only change), and ``apply_mode`` (``hot``, ``restart``, or
        ``none``).  The revision comparison intentionally happens only after
        acquiring SQLite's write lock.
        """
        sets: dict[str, Any] = {}
        resets: list[str] = []
        for change in changes:
            key = str(change.get("key", ""))
            action = change.get("action", "set")
            definition = SETTING_REGISTRY.get(key)
            if definition is None:
                raise ValueError(f"unknown setting: {key}")
            if action == "set":
                if "value" not in change:
                    raise ValueError(f"missing value for setting: {key}")
                sets[key] = change["value"]
            elif action == "clear":
                if not definition.secret:
                    raise ValueError("clear is only supported for secret settings")
                sets[key] = ""
            elif action == "reset":
                resets.append(key)
            else:
                raise ValueError(f"invalid setting action: {action}")
        all_keys = list(sets) + resets
        if not all_keys:
            return {"revision": self.get_revision(), "request_id": None, "apply_mode": "none"}
        for key in all_keys:
            definition = SETTING_REGISTRY[key]
            if not definition.editable or (definition.env and os.getenv(definition.env) is not None):
                raise PermissionError(f"setting is read-only: {key}")
        validated = {key: _validate_value(key, value) for key, value in sets.items()}
        apply_mode = "hot" if all(SETTING_REGISTRY[key].apply_mode == "hot" for key in all_keys) else "restart"
        request_id: str | None = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = int(db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])
            if base_revision is not None and base_revision != current:
                db.rollback()
                raise RuntimeError("revision conflict")
            # Read the managed layer only after taking the write lock.  This
            # prevents two no-base writers from validating against stale
            # cross-field state and creating an invalid candidate.
            candidate_managed = self._managed_values_db(db)
            try:
                self._validate_candidate(
                    all_keys, validated, resets, managed=candidate_managed
                )
            except Exception:
                db.rollback()
                raise
            now = time.time()
            for key, value in validated.items():
                db.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            for key in resets:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            new_revision = current + 1
            db.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(new_revision),))
            if apply_mode == "restart" and request_restart:
                request_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO restart_requests(request_id,revision,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (request_id, new_revision, "queued", now, now),
                )
            db.commit()
        return {"revision": new_revision, "request_id": request_id, "apply_mode": apply_mode}

    def reset(self, keys: list[str], base_revision: int | None = None) -> int:
        """Remove managed overrides, exposing the lower-priority value again."""
        for key in keys:
            definition = SETTING_REGISTRY.get(key)
            if definition is None:
                raise ValueError(f"unknown setting: {key}")
            if not definition.editable or (definition.env and os.getenv(definition.env) is not None):
                raise PermissionError(f"setting is read-only: {key}")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = int(db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])
            if base_revision is not None and base_revision != current:
                db.rollback()
                raise RuntimeError("revision conflict")
            try:
                self._validate_candidate(
                    keys, {}, keys, managed=self._managed_values_db(db)
                )
            except Exception:
                db.rollback()
                raise
            db.executemany("DELETE FROM settings WHERE key=?", ((key,) for key in keys))
            new_revision = current + 1 if keys else current
            if keys:
                db.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(new_revision),))
            db.commit()
        return new_revision

    def request_restart(self, revision: int | None = None) -> str:
        request_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO restart_requests(request_id,revision,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (request_id, self.get_revision() if revision is None else revision, "queued", now, now),
            )
        return request_id

    def claim_queued_restart(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued restart for the bot."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM restart_requests WHERE status='queued' "
                "ORDER BY created_at, request_id LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = time.time()
            db.execute(
                "UPDATE restart_requests SET status='draining',updated_at=? WHERE request_id=? AND status='queued'",
                (now, row["request_id"]),
            )
            db.commit()
        result = dict(row)
        result.update(status="draining", updated_at=now)
        return result

    # Short alias for consumers polling from a bot loop.
    claim_restart = claim_queued_restart

    def update_restart(
        self,
        request_id: str,
        status: str,
        error: str | None = None,
        *,
        active_count: int | None = None,
        detail: str | None = None,
    ) -> None:
        allowed = {"queued", "draining", "forcing", "restarting", "applied", "failed"}
        if status not in allowed:
            raise ValueError(f"invalid restart status: {status}")
        with self._connect() as db:
            db.execute(
                "UPDATE restart_requests SET status=?, updated_at=?, error=?, "
                "active_count=COALESCE(?,active_count), detail=COALESCE(?,detail) WHERE request_id=?",
                (status, time.time(), error, active_count, detail, request_id),
            )

    def restart_status(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM restart_requests WHERE request_id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def heartbeat(self, component: str, *, boot_id: str | None = None, revision: int | None = None, status: str = "ok", detail: str = "") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO heartbeat(component,boot_id,revision,seen_at,status,detail) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET boot_id=excluded.boot_id,revision=excluded.revision,seen_at=excluded.seen_at,status=excluded.status,detail=excluded.detail",
                (component, boot_id, self.get_revision() if revision is None else revision, time.time(), status, detail),
            )

    def heartbeat_status(self, component: str = "bot") -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM heartbeat WHERE component=?", (component,)).fetchone()
        return dict(row) if row else None

    def snapshot(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Return effective values and source metadata for every field.

        When ``config_path`` is supplied, YAML and its sibling legacy ``.env``
        are read directly.  This keeps source reporting accurate without
        loading dotenv values into the process environment.
        """
        managed = self.managed_values()
        supplied: dict[str, Any] = dict(values or {})
        legacy_env: dict[str, str] = {}
        if config_path:
            config = Path(config_path)
            try:
                raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                raw = {}
            for key in SETTING_REGISTRY:
                section, field = key.split(".", 1)
                current: Any = raw.get(section, {})
                for part in field.split("."):
                    current = current.get(part) if isinstance(current, Mapping) else None
                if current is not None:
                    supplied[key] = current
            legacy_env = read_dotenv(config.parent / ".env")
        result: dict[str, Any] = {}
        for key, definition in SETTING_REGISTRY.items():
            if definition.env and os.getenv(definition.env) is not None:
                source = "env"
                value = os.getenv(definition.env)
            elif key in managed:
                source = "managed"
                value = managed[key]
            elif definition.env and definition.env in legacy_env:
                source = "legacy_env"
                value = legacy_env[definition.env]
            elif key in supplied:
                source = "yaml"
                value = supplied[key]
            else:
                source = "default"
                value = DEFAULT_VALUES.get(key)
            if not definition.secret:
                try:
                    value = _coerce(value, definition.value_type)
                except ValueError:
                    # Keep visibility useful even if a lower-priority legacy
                    # source is malformed; config_loader applies its normal
                    # fallback rules when constructing AppConfig.
                    pass
            result[key] = {
                "value": None if definition.secret else value,
                "configured": bool(value) if definition.secret else value is not None,
                "source": source,
                "editable": definition.editable and not (definition.env and os.getenv(definition.env) is not None),
                "apply_mode": definition.apply_mode,
                "secret": definition.secret,
            }
        return result
