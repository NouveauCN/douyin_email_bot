"""Configuration loader using dataclasses, YAML, and environment variables.

Sensitive values (email password, douyin cookie) are loaded from
environment variables (typically via .env file). All other settings
come from config.yaml with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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
    commands: BotCommands = field(default_factory=BotCommands)


@dataclass
class CookieExtractorConfig:
    """Headless Firefox cookie extraction settings."""

    profile_dir: str = ""      # empty = use default ~/.wechat_video_download/firefox_profile/
    headless: bool = True      # run browser in headless mode
    validate: bool = True      # validate cookies after extraction


@dataclass
class AppConfig:
    """Top-level application config."""

    email: EmailConfig
    douyin: DouyinConfig
    bot: BotConfig
    cookie_extractor: CookieExtractorConfig


def load_config(path: Path) -> AppConfig:
    """Load configuration from YAML file, with secrets from env vars.

    Priority:
        1. Environment variables for secrets (EMAIL_ADDRESS, EMAIL_PASSWORD, DOUYIN_COOKIE)
        2. YAML file values for the same fields (fallback)
        3. Dataclass defaults for everything else
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
        poll_interval=email_raw.get("poll_interval", 30),
    )

    # ── Douyin ──
    douyin_raw = raw.get("douyin", {})
    douyin = DouyinConfig(
        download_path=douyin_raw.get("download_path", "./downloads"),
        cookie=os.getenv("DOUYIN_COOKIE") or douyin_raw.get("cookie", ""),
        naming=douyin_raw.get("naming", "{create}_{aweme_id}"),
        folderize=douyin_raw.get("folderize", True),
        timeout=douyin_raw.get("timeout", 30),
        max_retries=douyin_raw.get("max_retries", 3),
        max_tasks=douyin_raw.get("max_tasks", 5),
    )

    # ── Bot ──
    bot_raw = raw.get("bot", {})
    cmd_raw = bot_raw.get("commands", {})
    bot_commands = BotCommands(
        cookie_update=cmd_raw.get("cookie_update", "更新cookie"),
        cookie_auto=cmd_raw.get("cookie_auto", "自动获取cookie"),
    )
    bot = BotConfig(
        allowed_senders=bot_raw.get("allowed_senders", []),
        cooldown_seconds=bot_raw.get("cooldown_seconds", 5),
        subject_keyword=bot_raw.get("subject_keyword", "下载"),
        commands=bot_commands,
    )

    # ── Cookie Extractor ──
    extractor_raw = raw.get("cookie_extractor", {})
    cookie_extractor = CookieExtractorConfig(
        profile_dir=extractor_raw.get("profile_dir", ""),
        headless=extractor_raw.get("headless", True),
        validate=extractor_raw.get("validate", True),
    )

    return AppConfig(
        email=email, douyin=douyin, bot=bot, cookie_extractor=cookie_extractor
    )
