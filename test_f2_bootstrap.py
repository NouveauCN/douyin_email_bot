"""Focused checks for the F2 bootstrap and the live smoke entry point."""

import os
from pathlib import Path
import subprocess
import sys


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
