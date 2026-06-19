"""WeChat Bot for downloading Douyin videos.

Usage:
    uv run python main.py
"""

import logging
import sys
from pathlib import Path

from config_loader import load_config
from bot import WcfBot


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
        log.error("Copy and edit config.yaml (set douyin.cookie at minimum)")
        sys.exit(1)

    config = load_config(config_path)

    if not config.douyin.cookie:
        log.warning("douyin.cookie is empty — downloads will fail until set")
        log.info("To get a cookie: uv run f2 dy --auto-cookie chrome")

    bot = WcfBot(config)

    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C)")
    except Exception:
        log.exception("Bot crashed with unexpected error")
    finally:
        bot.cleanup()


if __name__ == "__main__":
    main()
