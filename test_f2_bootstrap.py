"""Focused checks for the F2 bootstrap and the live smoke entry point."""

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


PROJECT_DIR = Path(__file__).parent


def test_smoke_download_import_has_no_runtime_side_effects(tmp_path):
    """Importing the manual smoke command must not initialize F2 or config."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import smoke_download; "
            "assert not any(name == 'f2' or name.startswith('f2.') "
            "for name in sys.modules); print('safe')",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe"
    assert not (tmp_path / "conf").exists()


def test_bootstrap_f2_is_idempotent():
    """Repeated startup calls do not wrap F2 methods more than once."""
    from f2_bootstrap import bootstrap_f2

    bootstrap_f2()
    from f2.apps.bark.utils import ClientConfManager as BarkConfManager
    from f2.apps.douyin.utils import ClientConfManager, TokenManager

    methods = (
        ClientConfManager.brm_os.__func__,
        ClientConfManager.brm_version.__func__,
        ClientConfManager.brm_browser.__func__,
        ClientConfManager.brm_engine.__func__,
        TokenManager.gen_real_msToken.__func__,
        BarkConfManager.merge.__func__,
    )
    bootstrap_f2()
    assert methods == (
        ClientConfManager.brm_os.__func__,
        ClientConfManager.brm_version.__func__,
        ClientConfManager.brm_browser.__func__,
        ClientConfManager.brm_engine.__func__,
        TokenManager.gen_real_msToken.__func__,
        BarkConfManager.merge.__func__,
    )


def test_bootstrap_preserves_existing_f2_config(tmp_path):
    from f2_bootstrap import _ensure_config

    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    custom_conf = "f2:\n  enable_bark: true\n  custom: keep-me\n"
    custom_app = "bark:\n  token: keep-me\n"
    (conf_dir / "conf.yaml").write_text(custom_conf, encoding="utf-8")
    (conf_dir / "app.yaml").write_text(custom_app, encoding="utf-8")

    _ensure_config(tmp_path)

    assert (conf_dir / "conf.yaml").read_text(encoding="utf-8") == custom_conf
    assert (conf_dir / "app.yaml").read_text(encoding="utf-8") == custom_app


def test_bootstrap_uses_coherent_firefox_web_identity():
    from f2.apps.douyin.utils import ClientConfManager

    browser = ClientConfManager.brm_browser()
    engine = ClientConfManager.brm_engine()
    assert browser["name"] == "Firefox"
    assert browser["platform"] == "Linux x86_64"
    assert browser["version"] == engine["version"]
    assert engine["name"] == "Gecko"


def test_firefox_version_discovers_bounded_playwright_cache(tmp_path, monkeypatch):
    import f2_bootstrap

    executable = tmp_path / "firefox-1538" / "firefox" / "firefox"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    f2_bootstrap._FIREFOX_VERSION_CACHE.clear()
    with patch.object(f2_bootstrap.subprocess, "run") as run:
        run.return_value.stdout = "Mozilla Firefox 153.0"
        assert f2_bootstrap.firefox_version() == "153.0"
        assert f2_bootstrap.firefox_user_agent("153.0").endswith(
            "rv:153.0) Gecko/20100101 Firefox/153.0"
        )
        assert run.call_count >= 1
