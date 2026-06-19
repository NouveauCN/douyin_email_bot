"""Email Bot for downloading Douyin videos.

Monitors an IMAP inbox for emails containing Douyin share links,
downloads the videos, and replies with the result via SMTP.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

# ── Suppress noisy third-party logs BEFORE they are imported ──────
for _name in ("httpx", "httpcore", "f2", "browser_cookie3", "urllib3"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _lg.handlers.clear()
    _lg.propagate = False

from dotenv import load_dotenv  # noqa: E402

from config_loader import load_config  # noqa: E402
from email_bot import EmailBot  # noqa: E402


def setup_logging() -> None:
    """Configure root logger for our own output."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    setup_logging()

    # Second pass — F2 reconfigures its logger during import, suppress again
    for _name in ("httpx", "httpcore", "f2", "browser_cookie3", "urllib3"):
        _lg = logging.getLogger(_name)
        _lg.setLevel(logging.WARNING)
        _lg.handlers.clear()
        _lg.propagate = False

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
        log.info("To get a cookie: uv run python get_cookie.py")

    bot = EmailBot(config)

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C)")


if __name__ == "__main__":
    main()
