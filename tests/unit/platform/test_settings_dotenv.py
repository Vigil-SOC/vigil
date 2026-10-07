"""Import-time Settings must not read a developer's root .env (#577)."""

import os
import subprocess
import sys
import textwrap

import pytest

from core.config import REPO_ROOT, _settings_env_file


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("VIGIL_DISABLE_DOTENV", None)
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH")
        else str(REPO_ROOT)
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.mark.unit
def test_settings_env_file_skips_dotenv_when_disabled(monkeypatch):
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")
    assert _settings_env_file() is None


@pytest.mark.unit
def test_settings_env_file_loads_repo_root_dotenv_by_default(monkeypatch):
    monkeypatch.delenv("VIGIL_DISABLE_DOTENV", raising=False)
    assert _settings_env_file() == REPO_ROOT / ".env"


@pytest.mark.unit
def test_conftest_disables_dotenv_before_settings_import():
    """pytest loads tests/conftest.py before collecting modules that capture
    get_settings() at import time. The autouse fixture is too late; this
    subprocess imports conftest the same way collection does, without running
    fixtures, and checks Settings was defined with env_file already off.
    """
    script = textwrap.dedent("""\
        import tests.conftest  # noqa: F401
        from core.config import Settings

        env_file = Settings.model_config.get("env_file")
        assert env_file is None, f"env_file still {env_file!r}"
        print("ok")
        """)
    result = _run_isolated(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.unit
def test_settings_loads_repo_dotenv_when_marker_absent():
    """Production import must still point at the root .env (#520)."""
    script = textwrap.dedent("""\
        from core.config import REPO_ROOT, Settings

        env_file = Settings.model_config.get("env_file")
        assert env_file == REPO_ROOT / ".env", f"env_file is {env_file!r}"
        print("ok")
        """)
    result = _run_isolated(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
