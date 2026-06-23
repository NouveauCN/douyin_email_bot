"""Quick test: download a single Douyin video via command line."""

import logging
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ── Bootstrap: same patches as main.py ──────────────────────────
_F2_CONF_DIR = Path(__file__).parent / "conf"
_F2_CONF_DIR.mkdir(exist_ok=True)
_F2_CONF_FILE = _F2_CONF_DIR / "conf.yaml"
if not _F2_CONF_FILE.exists():
    _F2_CONF_FILE.write_text("f2:\n  enable_bark: false\n", encoding="utf-8")
(_F2_CONF_DIR / "app.yaml").write_text("bark: {}\n", encoding="utf-8")

# Monkey-patch F2 ClientConfManager
import f2.apps.douyin.utils as _douyin_utils  # noqa: E402
_DOUYIN_CCM = _douyin_utils.ClientConfManager

_orig_brm_os = _DOUYIN_CCM.brm_os.__func__
_orig_brm_version = _DOUYIN_CCM.brm_version.__func__
_orig_brm_browser = _DOUYIN_CCM.brm_browser.__func__
_orig_brm_engine = _DOUYIN_CCM.brm_engine.__func__

# Platform-aware fallback values (same as main.py)
_PLATFORM = "Linux" if sys.platform.startswith("linux") else (
    "Darwin" if sys.platform == "darwin" else "Windows"
)
_BROWSER_NAME = "Firefox" if _PLATFORM == "Linux" else "Edge"
_BROWSER_PLATFORM = "Linux x86_64" if _PLATFORM == "Linux" else (
    "MacIntel" if _PLATFORM == "Darwin" else "Win32"
)

@classmethod  # noqa: F811
def _safe_brm_os(cls):
    v = _orig_brm_os(cls)
    return v if isinstance(v, dict) else {"name": _PLATFORM, "version": "10"}

@classmethod
def _safe_brm_version(cls):
    v = _orig_brm_version(cls)
    return v if isinstance(v, dict) else {"code": "290100", "name": "29.1.0"}

@classmethod
def _safe_brm_browser(cls):
    v = _orig_brm_browser(cls)
    return v if isinstance(v, dict) else {
        "name": _BROWSER_NAME, "version": "130.0.0.0",
        "language": "zh-CN", "platform": _BROWSER_PLATFORM,
    }

@classmethod
def _safe_brm_engine(cls):
    v = _orig_brm_engine(cls)
    return v if isinstance(v, dict) else {"name": "Blink", "version": "130.0.0.0"}

_DOUYIN_CCM.brm_os = _safe_brm_os
_DOUYIN_CCM.brm_version = _safe_brm_version
_DOUYIN_CCM.brm_browser = _safe_brm_browser
_DOUYIN_CCM.brm_engine = _safe_brm_engine

_TokenManager = _douyin_utils.TokenManager
_orig_gen_real = _TokenManager.gen_real_msToken.__func__

@classmethod
def _safe_gen_real_msToken(cls):
    try:
        return _orig_gen_real(cls)
    except Exception:
        return cls.gen_false_msToken()

_TokenManager.gen_real_msToken = _safe_gen_real_msToken

# Patch Bark merge() to handle empty configs gracefully
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

# ── Now safe to import the rest ─────────────────────────────────
from dotenv import load_dotenv  # noqa: E402
from config_loader import load_config  # noqa: E402
from douyin_downloader import DouyinDownloader  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger("test")

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

config = load_config(Path(__file__).parent / "config.yaml")

if not config.douyin.cookie:
    log.error("DOUYIN_COOKIE is empty — run: uv run python get_cookie.py")
    sys.exit(1)

log.info("Cookie loaded (%d chars)", len(config.douyin.cookie))

# ── Download ────────────────────────────────────────────────────
url = "https://v.douyin.com/PHZUsa2ahtc/"
downloader = DouyinDownloader(config.douyin)

log.info(f"{Fore.CYAN}Testing: %s", url)
result = downloader.download(url)

if result["success"]:
    print(f"\n{Fore.GREEN}{Style.BRIGHT}[DONE] DOWNLOAD SUCCESS")
    print(f"  Title: {result['title']}")
    print(f"  File:  {result['filepath']}")
else:
    print(f"\n{Fore.RED}X DOWNLOAD FAILED")
    print(f"  Error: {result['error']}")
    sys.exit(1)
