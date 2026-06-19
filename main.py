"""Email Bot for downloading Douyin videos.

Monitors an IMAP inbox for emails containing Douyin share links,
downloads the videos, and replies with the result via SMTP.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

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

    # Load .env file (secrets: EMAIL_ADDRESS, EMAIL_PASSWORD, DOUYIN_COOKIE)
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        log.warning(".env file not found — copy .env.example to .env and fill in your secrets")

    # Load config.yaml (non-sensitive settings)
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        log.error("config.yaml not found in project directory")
        sys.exit(1)

    config = load_config(config_path)

    # Validate required secrets
    if not config.email.email or not config.email.password:
        log.error("EMAIL_ADDRESS and EMAIL_PASSWORD are required")
        log.error("Copy .env.example to .env and fill in your email credentials")
        sys.exit(1)

    if not config.douyin.cookie:
        log.warning("DOUYIN_COOKIE is empty — downloads will fail until set")
        log.info("Add DOUYIN_COOKIE to .env (see .env.example)")
        log.info("To get a cookie: uv run f2 dy --auto-cookie chrome")

    bot = EmailBot(config)

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C)")


if __name__ == "__main__":
    main()
