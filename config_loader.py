"""Configuration loader using dataclasses and YAML."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class EmailConfig:
    """Email IMAP/SMTP settings."""

    imap_server: str = "imap.qq.com"
    imap_port: int = 993
    smtp_server: str = "smtp.qq.com"
    smtp_port: int = 587
    email: str = ""       # Bot email address
    password: str = ""    # QQ authorization code (not login password)
    # Polling interval in seconds
    poll_interval: int = 30


@dataclass
class DouyinConfig:
    """Douyin download settings."""

    download_path: str = "./downloads"
    cookie: str = ""
    naming: str = "{create}_{aweme_id}"
    folderize: bool = True
    timeout: int = 30
    max_retries: int = 3
    max_tasks: int = 5


@dataclass
class BotConfig:
    """Bot behavior settings."""

    # Allowed sender email addresses (empty = allow all senders)
    allowed_senders: list[str] = field(default_factory=list)
    # Cooldown per sender (seconds)
    cooldown_seconds: int = 5
    # Subject must contain this keyword to trigger download
    subject_keyword: str = "下载"


@dataclass
class AppConfig:
    """Top-level application config."""

    email: EmailConfig
    douyin: DouyinConfig
    bot: BotConfig


def load_config(path: Path) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    email_raw = raw.get("email", {})
    email = EmailConfig(
        imap_server=email_raw.get("imap_server", "imap.qq.com"),
        imap_port=email_raw.get("imap_port", 993),
        smtp_server=email_raw.get("smtp_server", "smtp.qq.com"),
        smtp_port=email_raw.get("smtp_port", 587),
        email=email_raw.get("email", ""),
        password=email_raw.get("password", ""),
        poll_interval=email_raw.get("poll_interval", 30),
    )

    douyin_raw = raw.get("douyin", {})
    douyin = DouyinConfig(
        download_path=douyin_raw.get("download_path", "./downloads"),
        cookie=douyin_raw.get("cookie", ""),
        naming=douyin_raw.get("naming", "{create}_{aweme_id}"),
        folderize=douyin_raw.get("folderize", True),
        timeout=douyin_raw.get("timeout", 30),
        max_retries=douyin_raw.get("max_retries", 3),
        max_tasks=douyin_raw.get("max_tasks", 5),
    )

    bot_raw = raw.get("bot", {})
    bot = BotConfig(
        allowed_senders=bot_raw.get("allowed_senders", []),
        cooldown_seconds=bot_raw.get("cooldown_seconds", 5),
        subject_keyword=bot_raw.get("subject_keyword", "下载"),
    )

    return AppConfig(email=email, douyin=douyin, bot=bot)
