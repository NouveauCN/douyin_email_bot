"""Email Bot for downloading Douyin videos.

Monitors an IMAP inbox for emails containing Douyin share links,
downloads the videos, and replies with the result via SMTP.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

# ── Initialize colorama for Windows console color support ──────────
colorama_init(autoreset=True)

# ── Disable F2's Bark notification BEFORE F2 is imported ──────────
# F2 reads config at module import time.  Its default config enables
# Bark (api.day.app) which causes a 405 error on every download.
_F2_CONF_DIR = Path(__file__).parent / "conf"
_F2_CONF_DIR.mkdir(exist_ok=True)
_F2_CONF_FILE = _F2_CONF_DIR / "conf.yaml"
if not _F2_CONF_FILE.exists():
    _F2_CONF_FILE.write_text("f2:\n  enable_bark: false\n", encoding="utf-8")
(_F2_CONF_DIR / "app.yaml").write_text("bark: {}\n", encoding="utf-8")

# ── Patch F2's douyin ClientConfManager for missing config ─────────
# F2 expects a fully populated conf.yaml with BaseRequestModel etc.
# When config is missing, brm_os/brm_version/brm_browser/brm_engine
# return a str instead of dict, which crashes pydantic model defaults.
# This monkey-patch ensures those accessors always return a dict.
import f2.apps.douyin.utils as _douyin_utils  # noqa: E402

_DOUYIN_CCM = _douyin_utils.ClientConfManager

_orig_brm_os = _DOUYIN_CCM.brm_os.__func__
_orig_brm_version = _DOUYIN_CCM.brm_version.__func__
_orig_brm_browser = _DOUYIN_CCM.brm_browser.__func__
_orig_brm_engine = _DOUYIN_CCM.brm_engine.__func__

@classmethod
def _safe_brm_os(cls):
    v = _orig_brm_os(cls)
    return v if isinstance(v, dict) else {"name": "Windows", "version": "10"}

@classmethod
def _safe_brm_version(cls):
    v = _orig_brm_version(cls)
    return v if isinstance(v, dict) else {"code": "290100", "name": "29.1.0"}

@classmethod
def _safe_brm_browser(cls):
    v = _orig_brm_browser(cls)
    return v if isinstance(v, dict) else {"name": "Edge", "version": "130.0.0.0",
                                            "language": "zh-CN", "platform": "Win32"}

@classmethod
def _safe_brm_engine(cls):
    v = _orig_brm_engine(cls)
    return v if isinstance(v, dict) else {"name": "Blink", "version": "130.0.0.0"}

_DOUYIN_CCM.brm_os = _safe_brm_os
_DOUYIN_CCM.brm_version = _safe_brm_version
_DOUYIN_CCM.brm_browser = _safe_brm_browser
_DOUYIN_CCM.brm_engine = _safe_brm_engine

# F2 0.0.1.7 has a bug: the except handler calls gen_real_msToken() again
# instead of gen_false_msToken().  Also TokenManager.token_conf may be
# empty when conf.yaml lacks msToken keys, causing KeyError on "magic".
# Patch gen_real_msToken to fall back gracefully.
_TokenManager = _douyin_utils.TokenManager
_orig_gen_real = _TokenManager.gen_real_msToken.__func__

@classmethod
def _safe_gen_real_msToken(cls):
    try:
        return _orig_gen_real(cls)
    except Exception:
        return cls.gen_false_msToken()

_TokenManager.gen_real_msToken = _safe_gen_real_msToken

# BarkClientConfManager.merge() calls merge_config() which raises
# ValueError when both bark configs are empty (which is our case since
# we disabled Bark).  Patch merge() to return {} gracefully.
import f2.apps.bark.utils as _bark_utils  # noqa: E402
_orig_bark_merge = _bark_utils.ClientConfManager.merge.__func__

@classmethod
def _safe_bark_merge(cls):
    c = cls.client()
    a = cls.app()
    if not c and not a:
        return {}
    return _orig_bark_merge(cls)

_bark_utils.ClientConfManager.merge = _safe_bark_merge

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
