"""Email Bot for downloading Douyin videos.

Monitors an IMAP inbox for emails containing Douyin share links,
downloads the videos, and replies with the result via SMTP.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

from config_loader import load_config
from email_bot import EmailBot


def setup_logging() -> None:
    """Configure root logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    setup_logging()
    log = logging.getLogger("main")

    # Locate config
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        log.error("config.yaml not found in project directory")
        log.error("Copy and edit config.yaml (set email and douyin.cookie)")
        sys.exit(1)

    config = load_config(config_path)

    # Validate required settings
    if not config.email.email or not config.email.password:
        log.error("email.email and email.password are required in config.yaml")
        log.error("password is the QQ Mail authorization code, not your QQ password")
        sys.exit(1)

    if not config.douyin.cookie:
        log.warning("douyin.cookie is empty — downloads will fail until set")
        log.info("To get a cookie: uv run f2 dy --auto-cookie chrome")

    bot = EmailBot(config)

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C)")


if __name__ == "__main__":
    main()
