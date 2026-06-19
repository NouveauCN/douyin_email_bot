"""Configuration loader using dataclasses and YAML."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class WechatConfig:
    """WeChatFerry connection settings."""

    host: str | None = None
    port: int = 10086
    debug: bool = False


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

    message_delay: float = 1.0
    allowed_senders: list[str] = field(default_factory=list)
    cooldown_seconds: int = 5


@dataclass
class AppConfig:
    """Top-level application config."""

    wechat: WechatConfig
    douyin: DouyinConfig
    bot: BotConfig


def load_config(path: Path) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    wechat_raw = raw.get("wechat", {})
    wechat = WechatConfig(
        host=wechat_raw.get("host"),
        port=wechat_raw.get("port", 10086),
        debug=wechat_raw.get("debug", False),
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
        message_delay=bot_raw.get("message_delay", 1.0),
        allowed_senders=bot_raw.get("allowed_senders", []),
        cooldown_seconds=bot_raw.get("cooldown_seconds", 5),
    )

    return AppConfig(wechat=wechat, douyin=douyin, bot=bot)
