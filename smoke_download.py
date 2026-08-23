"""Download one configured Douyin URL for a manual smoke check.

This module is intentionally safe to import.  Use ``uv run python
smoke_download.py`` to perform the live download.
"""

import logging
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

from f2_bootstrap import bootstrap_f2


_URL = "https://v.douyin.com/PHZUsa2ahtc/"


def main() -> int:
    """Run the live smoke download and return a process exit status."""
    colorama_init(autoreset=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("smoke_download")

    # F2 must be configured before importing the downloader.  Keeping this in
    # main also means importing this script never reads secrets or contacts
    # the network.
    bootstrap_f2()
    from dotenv import load_dotenv
    from config_loader import load_config
    from douyin_downloader import DouyinDownloader

    project_dir = Path(__file__).parent
    env_path = project_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config = load_config(project_dir / "config.yaml")
    if not config.douyin.cookie:
        log.error("DOUYIN_COOKIE is empty — run: uv run python get_cookie.py")
        return 1

    log.info("Cookie loaded (%d chars)", len(config.douyin.cookie))
    downloader = DouyinDownloader(config.douyin)

    log.info("%sTesting: %s", Fore.CYAN, _URL)
    result = downloader.download(_URL)

    if result["success"]:
        print(f"\n{Fore.GREEN}{Style.BRIGHT}[DONE] DOWNLOAD SUCCESS")
        print(f"  Title: {result['title']}")
        print(f"  File:  {result['filepath']}")
        return 0

    print(f"\n{Fore.RED}X DOWNLOAD FAILED")
    print(f"  Error: {result['error']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
