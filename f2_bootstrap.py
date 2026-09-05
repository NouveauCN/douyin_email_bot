"""Apply the small F2 compatibility patch required by the bot.

F2 reads its configuration while importing its Douyin modules, so callers must
invoke :func:`bootstrap_f2` before importing ``email_bot`` or
``douyin_downloader``.  Keeping this work in a function makes command-line
smoke tools safe to import and allows repeated calls without stacking patches.
"""

from pathlib import Path
import os
import re
import shutil
import subprocess
import sys


_BOOTSTRAPPED = False
_FIREFOX_VERSION_CACHE: dict[str, str] = {}


def _firefox_executables() -> list[Path]:
    """Find Firefox and Playwright's bundled Firefox in bounded locations."""
    paths: list[Path] = []
    for name in ("firefox", "firefox-esr"):
        resolved = shutil.which(name)
        if resolved:
            paths.append(Path(resolved))
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    roots: list[Path] = []
    if configured and configured != "0":
        roots.append(Path(configured).expanduser())
    # Playwright's documented per-user cache and the common Docker root.
    roots.extend((Path.home() / ".cache" / "ms-playwright", Path("/ms-playwright")))
    for root in dict.fromkeys(roots):
        if root.is_dir():
            paths.extend(sorted(root.glob("firefox-*/firefox/firefox"), reverse=True))
    return list(dict.fromkeys(paths))


def firefox_version() -> str:
    """Return the installed Firefox major/minor version for request identity."""
    configured = os.getenv("DOUYIN_FIREFOX_VERSION", "").strip()
    if configured:
        candidates = [configured]
    else:
        cache_key = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
        if cache_key in _FIREFOX_VERSION_CACHE:
            return _FIREFOX_VERSION_CACHE[cache_key]
        candidates = []
        for path in _firefox_executables():
            try:
                output = subprocess.run(
                    [str(path), "--version"], capture_output=True, text=True,
                    timeout=2, check=False,
                ).stdout
                candidates.append(output)
            except (OSError, subprocess.SubprocessError):
                pass
    for value in candidates:
        match = re.search(r"Firefox\s+(\d+(?:\.\d+){0,3})", value, re.I)
        if not match and configured:
            match = re.fullmatch(r"\d+(?:\.\d+){0,3}", value)
        if match:
            parts = match.group(1).split(".")
            # Keep the browser's native two-component form; padding to four
            # components creates a fingerprint that Firefox itself doesn't
            # advertise (e.g. 153.0.0.0).
            if len(parts) < 2:
                parts.append("0")
            version = ".".join(parts[:4])
            if not configured:
                _FIREFOX_VERSION_CACHE[cache_key] = version
            return version
    return "130.0"


def firefox_user_agent(version: str | None = None) -> str:
    native_version = version or firefox_version()
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{native_version}) Gecko/20100101 Firefox/{native_version}"


def _write_if_missing(path: Path, content: str) -> None:
    """Create minimal F2 config without overwriting user customization."""
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _ensure_config(project_dir: Path) -> None:
    conf_dir = project_dir / "conf"
    conf_dir.mkdir(exist_ok=True)
    _write_if_missing(conf_dir / "conf.yaml", "f2:\n  enable_bark: false\n")
    _write_if_missing(conf_dir / "app.yaml", "bark: {}\n")


def bootstrap_f2(project_dir: Path | None = None) -> None:
    """Prepare F2 and install the bot's compatibility patches.

    F2 is imported lazily here to preserve the required configuration-before-
    import ordering.  The function is idempotent for callers that invoke it
    from more than one entry point in the same process.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    project_dir = (project_dir or Path(__file__).parent).resolve()
    _ensure_config(project_dir)

    # Import only after the config files have been prepared.  F2's config
    # paths are relative to the process working directory, as in the original
    # entry points.
    import f2.apps.douyin.utils as _douyin_utils
    import f2.apps.bark.utils as _bark_utils

    douyin_ccm = _douyin_utils.ClientConfManager
    if not getattr(douyin_ccm, "_douyin_email_bot_patched", False):
        orig_brm_os = douyin_ccm.brm_os.__func__
        orig_brm_version = douyin_ccm.brm_version.__func__

        platform_name = "Linux" if sys.platform.startswith("linux") else (
            "Darwin" if sys.platform == "darwin" else "Windows"
        )
        @classmethod
        def safe_brm_os(cls):
            value = orig_brm_os(cls)
            return value if isinstance(value, dict) else {
                "name": platform_name,
                "version": "10",
            }

        @classmethod
        def safe_brm_version(cls):
            value = orig_brm_version(cls)
            return value if isinstance(value, dict) else {
                "code": "290100",
                "name": "29.1.0",
            }

        @classmethod
        def safe_brm_browser(cls):
            version = firefox_version()
            return {
                "name": "Firefox",
                "version": version,
                "language": "zh-CN",
                "platform": "Linux x86_64",
            }

        @classmethod
        def safe_brm_engine(cls):
            return {"name": "Gecko", "version": firefox_version()}

        douyin_ccm.brm_os = safe_brm_os
        douyin_ccm.brm_version = safe_brm_version
        douyin_ccm.brm_browser = safe_brm_browser
        douyin_ccm.brm_engine = safe_brm_engine
        douyin_ccm._douyin_email_bot_patched = True

    token_manager = _douyin_utils.TokenManager
    if not getattr(token_manager, "_douyin_email_bot_patched", False):
        orig_gen_real = token_manager.gen_real_msToken.__func__

        @classmethod
        def safe_gen_real_ms_token(cls):
            try:
                return orig_gen_real(cls)
            except Exception:
                return cls.gen_false_msToken()

        token_manager.gen_real_msToken = safe_gen_real_ms_token
        token_manager._douyin_email_bot_patched = True

    bark_ccm = _bark_utils.ClientConfManager
    if not getattr(bark_ccm, "_douyin_email_bot_patched", False):
        orig_bark_merge = bark_ccm.merge.__func__

        @classmethod
        def safe_bark_merge(cls):
            client = cls.client()
            app = cls.app()
            if not client and not app:
                return {}
            return orig_bark_merge(cls)

        bark_ccm.merge = safe_bark_merge
        bark_ccm._douyin_email_bot_patched = True

    _BOOTSTRAPPED = True
