"""Email Bot for downloading Douyin and Bilibili videos.

Monitors an IMAP inbox for emails containing supported share links,
downloads the videos, and replies with the result via SMTP.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init
from f2_bootstrap import bootstrap_f2

# ── Initialize colorama for Windows console color support ──────────
colorama_init(autoreset=True)

# F2 reads config while its modules import, so this must stay before the
# email bot/downloader imports below.
bootstrap_f2()

from dotenv import load_dotenv  # noqa: E402

from config_loader import load_config  # noqa: E402
from email_bot import EmailBot  # noqa: E402


def setup_logging() -> None:
    """Configure root logger with file + optional console output.

    File output always goes to logs/bot.log with ANSI codes stripped.
    Console output is only attached when running interactively (TTY).
    """
    import re
    from logging.handlers import RotatingFileHandler

    class _AnsiStrippingFormatter(logging.Formatter):
        """Strips colorama ANSI escape sequences for clean file output."""
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

        def format(self, record: logging.LogRecord) -> str:
            msg = super().format(record)
            return self._ANSI_RE.sub("", msg)

    # Ensure log directory exists
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    # File handler — always present, DEBUG level, ANSI-stripped
    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(_AnsiStrippingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    file_handler.setLevel(logging.DEBUG)

    handlers: list[logging.Handler] = [file_handler]

    # Console handler — only when running in an interactive terminal
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        console_handler.setLevel(logging.INFO)
        handlers.append(console_handler)

    logging.basicConfig(level=logging.DEBUG, handlers=handlers)


def main() -> None:
    setup_logging()

    log = logging.getLogger("main")

    # Load .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        log.warning(".env file not found — copy .env.example to .env and fill in your secrets")

    # Load config.yaml
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        log.error("config.yaml not found in project directory")
        sys.exit(1)

    config = load_config(config_path)

    if not config.email.email or not config.email.password:
        log.error("EMAIL_ADDRESS and EMAIL_PASSWORD are required")
        log.error("Copy .env.example to .env and fill in your email credentials")
        sys.exit(1)

    if not config.douyin.cookie:
        log.warning(f"{Fore.YELLOW}DOUYIN_COOKIE is empty — downloads will fail until set")
        log.info("Add DOUYIN_COOKIE to .env (see .env.example)")
        log.info("To get a cookie: uv run python get_cookie.py")
    else:
        # ── Startup cookie quality assessment ───────────────────────
        from cookie_extractor import _assess_quality  # noqa: E402
        grade, is_auth = _assess_quality(config.douyin.cookie)
        if is_auth:
            log.info(f"{Fore.GREEN}Cookie: %d 字符 — %s", len(config.douyin.cookie), grade)
        else:
            log.warning(
                f"{Fore.YELLOW}Cookie: %d 字符 — %s"
                f"{Style.RESET_ALL} (下载功能可能受限，建议运行: uv run python get_cookie.py)",
                len(config.douyin.cookie), grade,
            )

    bot = EmailBot(config)

    try:
        log.info(f"{Fore.CYAN}{Style.BRIGHT}EmailBot 启动中...")
        bot.run()
    except KeyboardInterrupt:
        log.info(f"{Fore.YELLOW}Bot stopped by user (Ctrl+C)")


if __name__ == "__main__":
    main()
