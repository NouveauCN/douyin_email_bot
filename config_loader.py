"""Configuration loader using dataclasses, YAML, and environment variables.

Sensitive values (email password, douyin cookie) are loaded from
environment variables (typically via .env file). All other settings
come from config.yaml with sensible defaults.

Priority: env var > YAML value > dataclass default.

Docker env-var overrides:
    DOUYIN_DOWNLOAD_PATH  — overrides douyin.download_path
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
    EMAIL_POLL_INTERVAL   — overrides email.poll_interval
    BOT_ALLOWED_SENDERS   — overrides bot.allowed_senders (comma-separated)
    BOT_COOLDOWN_SECONDS  — overrides bot.cooldown_seconds
    BOT_SUBJECT_KEYWORD   — overrides bot.subject_keyword
    BOT_TRANSIENT_RETRY_ATTEMPTS — overrides bot.transient_retry_attempts
    BOT_TRANSIENT_RETRY_DELAY_SECONDS — overrides bot.transient_retry_delay_seconds
    MEDIA_BACKUP_RETENTION_DAYS — overrides media_cleanup.backup_retention_days
    MEDIA_BACKUP_CHECK_INTERVAL_DAYS — overrides media_cleanup.check_interval_days
    COOKIE_PROFILE_DIR    — overrides cookie_extractor.profile_dir
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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


def _resolve_project_path(config_path: Path, value: str) -> str:
    """Resolve a project-relative path from config.yaml."""
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
    poll_interval: int = 30


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
class BotCommands:
    """Email subject keywords that trigger special actions."""

    cookie_update: str = "更新cookie"    # Paste new cookie in body
    cookie_auto: str = "自动获取cookie"   # Auto-extract from browser


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
    commands: BotCommands = field(default_factory=BotCommands)


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
    """Load configuration from YAML file, with secrets from env vars.

    Priority:
        1. Environment variables (os.getenv) — highest
        2. YAML file values for the same fields — fallback
        3. Dataclass defaults — lowest
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # ── Email ──
    email_raw = raw.get("email", {})
    email = EmailConfig(
        imap_server=email_raw.get("imap_server", "imap.qq.com"),
        imap_port=email_raw.get("imap_port", 993),
        smtp_server=email_raw.get("smtp_server", "smtp.qq.com"),
        smtp_port=email_raw.get("smtp_port", 587),
        email=os.getenv("EMAIL_ADDRESS") or email_raw.get("email", ""),
        password=os.getenv("EMAIL_PASSWORD") or email_raw.get("password", ""),
        poll_interval=_env_int(
            "EMAIL_POLL_INTERVAL",
            email_raw.get("poll_interval", 30),
        ),
    )

    # ── Douyin ──
    douyin_raw = raw.get("douyin", {})
    # Resolve relative download_path against config.yaml's directory
    # so downloads always land in the project tree regardless of CWD.
    # DOUYIN_DOWNLOAD_PATH env var takes precedence over YAML.
    _dl_path = Path(_env_str(
        "DOUYIN_DOWNLOAD_PATH",
        douyin_raw.get("download_path", "./downloads"),
    ))
    if not _dl_path.is_absolute():
        _dl_path = path.parent / _dl_path
    douyin = DouyinConfig(
        download_path=str(_dl_path.resolve()),
        cookie=os.getenv("DOUYIN_COOKIE") or douyin_raw.get("cookie", ""),
        naming=douyin_raw.get("naming", "{create}_{aweme_id}"),
        folderize=douyin_raw.get("folderize", True),
        timeout=_env_int("DOUYIN_TIMEOUT", douyin_raw.get("timeout", 30)),
        max_retries=_env_int("DOUYIN_MAX_RETRIES", douyin_raw.get("max_retries", 3)),
        max_tasks=_env_int("DOUYIN_MAX_TASKS", douyin_raw.get("max_tasks", 5)),
    )

    # ── Bilibili ──
    bilibili_raw = raw.get("bilibili", {})
    _bili_dl_path = Path(_env_str(
        "BILIBILI_DOWNLOAD_PATH",
        bilibili_raw.get("download_path", str(_dl_path / "bilibili")),
    ))
    if not _bili_dl_path.is_absolute():
        _bili_dl_path = path.parent / _bili_dl_path
    bilibili = BilibiliConfig(
        download_path=str(_bili_dl_path.resolve()),
        auth=os.getenv("BILIBILI_AUTH") or bilibili_raw.get("auth", ""),
        auth_file=_env_str("BILIBILI_AUTH_FILE", bilibili_raw.get("auth_file", "")),
        timeout=_env_int("BILIBILI_TIMEOUT", bilibili_raw.get("timeout", 3600)),
        batch=_env_bool("BILIBILI_BATCH", bilibili_raw.get("batch", False)),
        video_quality=_env_int(
            "BILIBILI_VIDEO_QUALITY",
            bilibili_raw.get("video_quality", 127),
        ),
        yutto_bin=_env_str("BILIBILI_YUTTO_BIN", bilibili_raw.get("yutto_bin", "yutto")),
    )

    # ── Bot ──
    bot_raw = raw.get("bot", {})
    cmd_raw = bot_raw.get("commands", {})
    bot_commands = BotCommands(
        cookie_update=cmd_raw.get("cookie_update", "更新cookie"),
        cookie_auto=cmd_raw.get("cookie_auto", "自动获取cookie"),
    )
    bot = BotConfig(
        allowed_senders=_parse_allowed_senders(
            os.getenv("BOT_ALLOWED_SENDERS")
            or bot_raw.get("allowed_senders", [])
        ),
        cooldown_seconds=_env_int(
            "BOT_COOLDOWN_SECONDS",
            bot_raw.get("cooldown_seconds", 5),
        ),
        subject_keyword=_env_str(
            "BOT_SUBJECT_KEYWORD",
            bot_raw.get("subject_keyword", "下载"),
        ),
        transient_retry_attempts=_env_int(
            "BOT_TRANSIENT_RETRY_ATTEMPTS",
            bot_raw.get("transient_retry_attempts", 3),
        ),
        transient_retry_delay_seconds=_env_int(
            "BOT_TRANSIENT_RETRY_DELAY_SECONDS",
            bot_raw.get("transient_retry_delay_seconds", 120),
        ),
        transient_pending_file=_resolve_project_path(
            path,
            bot_raw.get("transient_pending_file", "./pending_retries.json"),
        ),
        transient_failed_file=_resolve_project_path(
            path,
            bot_raw.get("transient_failed_file", "./failed_links.txt"),
        ),
        commands=bot_commands,
    )

    # ── Media backup cleanup ──
    cleanup_raw = raw.get("media_cleanup", {})
    media_cleanup = MediaCleanupConfig(
        backup_retention_days=max(
            1,
            _env_int(
                "MEDIA_BACKUP_RETENTION_DAYS",
                cleanup_raw.get("backup_retention_days", 28),
            ),
        ),
        check_interval_days=max(
            1,
            _env_int(
                "MEDIA_BACKUP_CHECK_INTERVAL_DAYS",
                cleanup_raw.get("check_interval_days", 7),
            ),
        ),
    )

    # ── Cookie Extractor ──
    extractor_raw = raw.get("cookie_extractor", {})
    cookie_extractor = CookieExtractorConfig(
        profile_dir=_env_str(
            "COOKIE_PROFILE_DIR",
            extractor_raw.get("profile_dir", ""),
        ),
        headless=extractor_raw.get("headless", True),
        validate=extractor_raw.get("validate", True),
    )

    return AppConfig(
        email=email,
        douyin=douyin,
        bilibili=bilibili,
        bot=bot,
        media_cleanup=media_cleanup,
        cookie_extractor=cookie_extractor,
    )
