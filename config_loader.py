"""Configuration loader using dataclasses, YAML, and managed settings.

Configuration priority is process environment > managed settings (the
SQLite runtime settings store) > legacy ``.env`` > YAML > dataclass default.
The legacy dotenv file is read as a fallback and is never loaded into
``os.environ`` by this module.

Environment variable mappings:
    RUNTIME_SETTINGS_DB — location of the managed settings SQLite database
    EMAIL_IMAP_SERVER / EMAIL_IMAP_PORT — email.imap_server / imap_port
    EMAIL_SMTP_SERVER / EMAIL_SMTP_PORT — email.smtp_server / smtp_port
    EMAIL_ADDRESS / EMAIL_PASSWORD — email.email / password
    EMAIL_SEND_REPLIES — email.send_replies
    EMAIL_POLL_INTERVAL / SMTP_TIMEOUT — email polling and SMTP timeout
    DOUYIN_DOWNLOAD_PATH  — overrides douyin.download_path
    DOUYIN_COOKIE         — overrides douyin.cookie
    DOUYIN_NAMING         — overrides douyin.naming
    DOUYIN_FOLDERIZE      — overrides douyin.folderize
    DOUYIN_TIMEOUT        — overrides douyin.timeout
    DOUYIN_MAX_RETRIES    — overrides douyin.max_retries
    DOUYIN_MAX_TASKS      — overrides douyin.max_tasks
    BILIBILI_DOWNLOAD_PATH — overrides bilibili.download_path
    BILIBILI_AUTH         — overrides bilibili.auth
    BILIBILI_AUTH_FILE    — overrides bilibili.auth_file
    BILIBILI_TIMEOUT      — overrides bilibili.timeout
    BILIBILI_BATCH        — overrides bilibili.batch
    BILIBILI_VIDEO_QUALITY — overrides bilibili.video_quality
    BILIBILI_YUTTO_BIN    — overrides bilibili.yutto_bin
    BOT_ALLOWED_SENDERS   — overrides bot.allowed_senders (comma-separated)
    BOT_COOLDOWN_SECONDS  — overrides bot.cooldown_seconds
    BOT_SUBJECT_KEYWORD   — overrides bot.subject_keyword
    BOT_TRANSIENT_RETRY_ATTEMPTS — overrides bot.transient_retry_attempts
    BOT_TRANSIENT_RETRY_DELAY_SECONDS — overrides bot.transient_retry_delay_seconds
    BOT_TRANSIENT_PENDING_FILE — overrides bot.transient_pending_file
    BOT_TRANSIENT_FAILED_FILE — overrides bot.transient_failed_file
    BOT_DURABLE_MAIL_ENABLED — overrides bot.durable_mail_enabled
    BOT_STATE_DB — overrides bot.state_db
    BOT_WORKER_COUNT — overrides bot.worker_count
    BOT_DOUYIN_WORKER_COUNT — overrides bot.douyin_worker_count
    BOT_BILIBILI_WORKER_COUNT — overrides bot.bilibili_worker_count
    BOT_LEASE_SECONDS — overrides bot.lease_seconds
    BOT_HEARTBEAT_SECONDS — overrides bot.heartbeat_seconds
    BOT_OUTBOX_RETRY_ATTEMPTS — overrides bot.outbox_retry_attempts
    BOT_OUTBOX_RETRY_DELAY_SECONDS — overrides bot.outbox_retry_delay_seconds
    MEDIA_BACKUP_RETENTION_DAYS — overrides media_cleanup.backup_retention_days
    MEDIA_BACKUP_CHECK_INTERVAL_DAYS — overrides media_cleanup.check_interval_days
    COOKIE_PROFILE_DIR / COOKIE_HEADLESS / COOKIE_VALIDATE — cookie extractor options
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from settings_store import SettingsStore, default_database_path, read_dotenv


def _env_int(name: str, fallback: int) -> int:
    val = os.getenv(name)
    if val is not None and val.strip():
        try:
            return int(val)
        except ValueError:
            pass
    return fallback


def _env_str(name: str, fallback: str) -> str:
    return os.getenv(name) or fallback


def _env_bool(name: str, fallback: bool) -> bool:
    val = os.getenv(name)
    if val is None or not val.strip():
        return fallback
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_allowed_senders(value) -> list[str]:
    """Parse allowed senders from env var (comma-separated) or YAML list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


def _setting_value(
    key: str,
    env_name: str | None,
    managed: dict,
    dotenv: dict[str, str],
    yaml_value,
    default,
):
    """Resolve one setting without mutating the process environment.

    ``main.py`` and the web entry points historically call ``load_dotenv``
    before loading config.  New callers should not do that; this helper still
    gives an already-present process environment the documented highest
    priority while reading a legacy dotenv file only as a fallback.
    """
    if env_name and env_name in os.environ:
        return os.environ[env_name]
    if key in managed:
        return managed[key]
    if env_name and env_name in dotenv:
        return dotenv[env_name]
    return yaml_value if yaml_value is not None else default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _resolve_project_path(config_path: Path, value: str | None) -> str:
    """Resolve a project-relative path from config.yaml.

    Empty optional paths stay empty instead of resolving to the directory
    containing the config file.
    """
    if not value:
        return ""
    target = Path(value)
    if not target.is_absolute():
        target = config_path.parent / target
    return str(target.resolve())


@dataclass
class EmailConfig:
    """Email IMAP/SMTP settings.

    password is loaded from EMAIL_PASSWORD env var, not YAML.
    """

    imap_server: str = "imap.qq.com"
    imap_port: int = 993
    smtp_server: str = "smtp.qq.com"
    smtp_port: int = 587
    email: str = ""       # From EMAIL_ADDRESS env var
    password: str = ""    # From EMAIL_PASSWORD env var
    send_replies: bool = True  # From EMAIL_SEND_REPLIES; tasks still run when false
    poll_interval: int = 30
    smtp_timeout: int = 30


@dataclass
class DouyinConfig:
    """Douyin download settings.

    cookie is loaded from DOUYIN_COOKIE env var, not YAML.
    """

    download_path: str = "./downloads"
    cookie: str = ""       # From DOUYIN_COOKIE env var
    naming: str = "{create}_{aweme_id}"
    folderize: bool = True
    timeout: int = 30
    max_retries: int = 3
    max_tasks: int = 5


@dataclass
class BilibiliConfig:
    """Bilibili download settings.

    auth is loaded from BILIBILI_AUTH env var, not YAML.
    """

    download_path: str = "./downloads/bilibili"
    auth: str = ""          # From BILIBILI_AUTH env var
    auth_file: str = ""     # From BILIBILI_AUTH_FILE env var
    timeout: int = 3600
    batch: bool = False
    video_quality: int = 127
    yutto_bin: str = "yutto"


@dataclass
class BotConfig:
    """Bot behavior settings."""

    allowed_senders: list[str] = field(default_factory=list)
    cooldown_seconds: int = 5
    subject_keyword: str = "下载"
    transient_retry_attempts: int = 3
    transient_retry_delay_seconds: int = 120
    transient_pending_file: str = "./pending_retries.json"
    transient_failed_file: str = "./failed_links.txt"
    # Durable mail processing is additive and can be disabled for rollback.
    durable_mail_enabled: bool = True
    state_db: str = "./state/mail_state.sqlite3"
    worker_count: int = 2
    douyin_worker_count: int = 1
    bilibili_worker_count: int = 1
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    outbox_retry_attempts: int = 5
    outbox_retry_delay_seconds: int = 60

    def __post_init__(self) -> None:
        self.worker_count = max(1, int(self.worker_count))
        self.douyin_worker_count = max(1, int(self.douyin_worker_count))
        self.bilibili_worker_count = max(1, int(self.bilibili_worker_count))
        self.lease_seconds = max(10, int(self.lease_seconds))
        self.heartbeat_seconds = min(
            max(1, int(self.heartbeat_seconds)),
            max(1, self.lease_seconds // 3),
        )
        self.outbox_retry_attempts = max(1, int(self.outbox_retry_attempts))
        self.outbox_retry_delay_seconds = max(1, int(self.outbox_retry_delay_seconds))


@dataclass
class CookieExtractorConfig:
    """Headless Firefox cookie extraction settings."""

    profile_dir: str = ""      # empty = use default ~/.douyin_email_bot/firefox_profile/
    headless: bool = True      # run browser in headless mode
    validate: bool = True      # validate cookies after extraction


@dataclass
class MediaCleanupConfig:
    """Retention policy for originals kept after media cropping."""

    backup_retention_days: int = 28
    check_interval_days: int = 7


@dataclass
class AppConfig:
    """Top-level application config."""

    email: EmailConfig
    douyin: DouyinConfig
    bilibili: BilibiliConfig
    bot: BotConfig
    media_cleanup: MediaCleanupConfig
    cookie_extractor: CookieExtractorConfig


def load_config(path: Path) -> AppConfig:
    """Load configuration from all supported sources.

    Priority:
        1. Process environment variables — highest
        2. Managed SQLite settings
        3. Legacy ``.env`` values (read without mutating ``os.environ``)
        4. YAML file values
        5. Dataclass defaults — lowest
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Do not call load_dotenv here: the file is a lower-priority source and
    # must not overwrite/poison os.environ for other code in this process.
    settings = SettingsStore(default_database_path(path))
    managed = settings.managed_values()
    dotenv = read_dotenv(path.parent / ".env")

    # ── Email ──
    email_raw = raw.get("email", {})
    email = EmailConfig(
        imap_server=_setting_value("email.imap_server", "EMAIL_IMAP_SERVER", managed, dotenv, email_raw.get("imap_server"), "imap.qq.com"),
        imap_port=int(_setting_value("email.imap_port", "EMAIL_IMAP_PORT", managed, dotenv, email_raw.get("imap_port"), 993)),
        smtp_server=_setting_value("email.smtp_server", "EMAIL_SMTP_SERVER", managed, dotenv, email_raw.get("smtp_server"), "smtp.qq.com"),
        smtp_port=int(_setting_value("email.smtp_port", "EMAIL_SMTP_PORT", managed, dotenv, email_raw.get("smtp_port"), 587)),
        email=_setting_value("email.email", "EMAIL_ADDRESS", managed, dotenv, email_raw.get("email"), ""),
        password=_setting_value("email.password", "EMAIL_PASSWORD", managed, dotenv, email_raw.get("password"), ""),
        send_replies=_as_bool(_setting_value("email.send_replies", "EMAIL_SEND_REPLIES", managed, dotenv, email_raw.get("send_replies"), True), True),
        poll_interval=_env_int("EMAIL_POLL_INTERVAL", int(_setting_value("email.poll_interval", "EMAIL_POLL_INTERVAL", managed, dotenv, email_raw.get("poll_interval"), 30))),
        smtp_timeout=max(1, _env_int("SMTP_TIMEOUT", int(_setting_value("email.smtp_timeout", "SMTP_TIMEOUT", managed, dotenv, email_raw.get("smtp_timeout"), 30)))),
    )

    # ── Douyin ──
    douyin_raw = raw.get("douyin", {})
    # Resolve relative download_path against config.yaml's directory
    # so downloads always land in the project tree regardless of CWD.
    # DOUYIN_DOWNLOAD_PATH env var takes precedence over YAML.
    _dl_path = Path(_setting_value("douyin.download_path", "DOUYIN_DOWNLOAD_PATH", managed, dotenv, douyin_raw.get("download_path"), "./downloads"))
    if not _dl_path.is_absolute():
        _dl_path = path.parent / _dl_path
    douyin = DouyinConfig(
        download_path=str(_dl_path.resolve()),
        cookie=_setting_value("douyin.cookie", "DOUYIN_COOKIE", managed, dotenv, douyin_raw.get("cookie"), ""),
        naming=_setting_value("douyin.naming", "DOUYIN_NAMING", managed, dotenv, douyin_raw.get("naming"), "{create}_{aweme_id}"),
        folderize=_as_bool(_setting_value("douyin.folderize", "DOUYIN_FOLDERIZE", managed, dotenv, douyin_raw.get("folderize"), True), True),
        timeout=_env_int("DOUYIN_TIMEOUT", int(_setting_value("douyin.timeout", "DOUYIN_TIMEOUT", managed, dotenv, douyin_raw.get("timeout"), 30))),
        max_retries=_env_int("DOUYIN_MAX_RETRIES", int(_setting_value("douyin.max_retries", "DOUYIN_MAX_RETRIES", managed, dotenv, douyin_raw.get("max_retries"), 3))),
        max_tasks=_env_int("DOUYIN_MAX_TASKS", int(_setting_value("douyin.max_tasks", "DOUYIN_MAX_TASKS", managed, dotenv, douyin_raw.get("max_tasks"), 5))),
    )

    # ── Bilibili ──
    bilibili_raw = raw.get("bilibili", {})
    _bili_dl_path = Path(_setting_value("bilibili.download_path", "BILIBILI_DOWNLOAD_PATH", managed, dotenv, bilibili_raw.get("download_path"), str(_dl_path / "bilibili")))
    if not _bili_dl_path.is_absolute():
        _bili_dl_path = path.parent / _bili_dl_path
    bilibili = BilibiliConfig(
        download_path=str(_bili_dl_path.resolve()),
        auth=_setting_value("bilibili.auth", "BILIBILI_AUTH", managed, dotenv, bilibili_raw.get("auth"), ""),
        auth_file=_resolve_project_path(
            path,
            _setting_value("bilibili.auth_file", "BILIBILI_AUTH_FILE", managed, dotenv, bilibili_raw.get("auth_file"), ""),
        ),
        timeout=_env_int("BILIBILI_TIMEOUT", int(_setting_value("bilibili.timeout", "BILIBILI_TIMEOUT", managed, dotenv, bilibili_raw.get("timeout"), 3600))),
        batch=_as_bool(_setting_value("bilibili.batch", "BILIBILI_BATCH", managed, dotenv, bilibili_raw.get("batch"), False), False),
        video_quality=_env_int("BILIBILI_VIDEO_QUALITY", int(_setting_value("bilibili.video_quality", "BILIBILI_VIDEO_QUALITY", managed, dotenv, bilibili_raw.get("video_quality"), 127))),
        yutto_bin=_setting_value("bilibili.yutto_bin", "BILIBILI_YUTTO_BIN", managed, dotenv, bilibili_raw.get("yutto_bin"), "yutto"),
    )

    # ── Bot ──
    bot_raw = raw.get("bot", {})
    bot = BotConfig(
        allowed_senders=_parse_allowed_senders(
            _setting_value("bot.allowed_senders", "BOT_ALLOWED_SENDERS", managed, dotenv, bot_raw.get("allowed_senders"), [])
        ),
        cooldown_seconds=_env_int("BOT_COOLDOWN_SECONDS", int(_setting_value("bot.cooldown_seconds", "BOT_COOLDOWN_SECONDS", managed, dotenv, bot_raw.get("cooldown_seconds"), 5))),
        subject_keyword=_setting_value("bot.subject_keyword", "BOT_SUBJECT_KEYWORD", managed, dotenv, bot_raw.get("subject_keyword"), "下载"),
        transient_retry_attempts=_env_int("BOT_TRANSIENT_RETRY_ATTEMPTS", int(_setting_value("bot.transient_retry_attempts", "BOT_TRANSIENT_RETRY_ATTEMPTS", managed, dotenv, bot_raw.get("transient_retry_attempts"), 3))),
        transient_retry_delay_seconds=_env_int("BOT_TRANSIENT_RETRY_DELAY_SECONDS", int(_setting_value("bot.transient_retry_delay_seconds", "BOT_TRANSIENT_RETRY_DELAY_SECONDS", managed, dotenv, bot_raw.get("transient_retry_delay_seconds"), 120))),
        transient_pending_file=_resolve_project_path(
            path,
            _setting_value("bot.transient_pending_file", "BOT_TRANSIENT_PENDING_FILE", managed, dotenv, bot_raw.get("transient_pending_file"), "./pending_retries.json"),
        ),
        transient_failed_file=_resolve_project_path(
            path,
            _setting_value("bot.transient_failed_file", "BOT_TRANSIENT_FAILED_FILE", managed, dotenv, bot_raw.get("transient_failed_file"), "./failed_links.txt"),
        ),
        durable_mail_enabled=_env_bool("BOT_DURABLE_MAIL_ENABLED", _as_bool(_setting_value("bot.durable_mail_enabled", "BOT_DURABLE_MAIL_ENABLED", managed, dotenv, bot_raw.get("durable_mail_enabled"), True), True)),
        state_db=_resolve_project_path(
            path,
            _setting_value("bot.state_db", "BOT_STATE_DB", managed, dotenv, bot_raw.get("state_db"), "./state/mail_state.sqlite3"),
        ),
        worker_count=max(1, _env_int("BOT_WORKER_COUNT", int(_setting_value("bot.worker_count", "BOT_WORKER_COUNT", managed, dotenv, bot_raw.get("worker_count"), 2)))),
        douyin_worker_count=max(1, _env_int("BOT_DOUYIN_WORKER_COUNT", int(_setting_value("bot.douyin_worker_count", "BOT_DOUYIN_WORKER_COUNT", managed, dotenv, bot_raw.get("douyin_worker_count"), 1)))),
        bilibili_worker_count=max(1, _env_int("BOT_BILIBILI_WORKER_COUNT", int(_setting_value("bot.bilibili_worker_count", "BOT_BILIBILI_WORKER_COUNT", managed, dotenv, bot_raw.get("bilibili_worker_count"), 1)))),
        lease_seconds=max(10, _env_int("BOT_LEASE_SECONDS", int(_setting_value("bot.lease_seconds", "BOT_LEASE_SECONDS", managed, dotenv, bot_raw.get("lease_seconds"), 300)))),
        heartbeat_seconds=max(1, _env_int("BOT_HEARTBEAT_SECONDS", int(_setting_value("bot.heartbeat_seconds", "BOT_HEARTBEAT_SECONDS", managed, dotenv, bot_raw.get("heartbeat_seconds"), 30)))),
        outbox_retry_attempts=max(1, _env_int("BOT_OUTBOX_RETRY_ATTEMPTS", int(_setting_value("bot.outbox_retry_attempts", "BOT_OUTBOX_RETRY_ATTEMPTS", managed, dotenv, bot_raw.get("outbox_retry_attempts"), 5)))),
        outbox_retry_delay_seconds=max(1, _env_int("BOT_OUTBOX_RETRY_DELAY_SECONDS", int(_setting_value("bot.outbox_retry_delay_seconds", "BOT_OUTBOX_RETRY_DELAY_SECONDS", managed, dotenv, bot_raw.get("outbox_retry_delay_seconds"), 60)))),
    )

    # ── Media backup cleanup ──
    cleanup_raw = raw.get("media_cleanup", {})
    media_cleanup = MediaCleanupConfig(
        backup_retention_days=max(
            1,
            _env_int("MEDIA_BACKUP_RETENTION_DAYS", int(_setting_value("media_cleanup.backup_retention_days", "MEDIA_BACKUP_RETENTION_DAYS", managed, dotenv, cleanup_raw.get("backup_retention_days"), 28))),
        ),
        check_interval_days=max(
            1,
            _env_int("MEDIA_BACKUP_CHECK_INTERVAL_DAYS", int(_setting_value("media_cleanup.check_interval_days", "MEDIA_BACKUP_CHECK_INTERVAL_DAYS", managed, dotenv, cleanup_raw.get("check_interval_days"), 7))),
        ),
    )

    # ── Cookie Extractor ──
    extractor_raw = raw.get("cookie_extractor", {})
    cookie_extractor = CookieExtractorConfig(
        profile_dir=_resolve_project_path(
            path,
            _setting_value("cookie_extractor.profile_dir", "COOKIE_PROFILE_DIR", managed, dotenv, extractor_raw.get("profile_dir"), ""),
        ),
        headless=_as_bool(_setting_value("cookie_extractor.headless", "COOKIE_HEADLESS", managed, dotenv, extractor_raw.get("headless"), True), True),
        validate=_as_bool(_setting_value("cookie_extractor.validate", "COOKIE_VALIDATE", managed, dotenv, extractor_raw.get("validate"), True), True),
    )

    return AppConfig(
        email=email,
        douyin=douyin,
        bilibili=bilibili,
        bot=bot,
        media_cleanup=media_cleanup,
        cookie_extractor=cookie_extractor,
    )
