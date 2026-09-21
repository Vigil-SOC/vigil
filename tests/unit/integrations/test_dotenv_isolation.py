"""A vendor tool server reads `.env` when it is one, and not when it is imported.

`core/integrations/<vendor>/tool.py` is spawned as its own program with a
narrowed environment, where `.env` is the only thing that tells it where the
vendor lives. Imported instead -- which the tests do -- `load_dotenv()` copies
the developer's whole file into `os.environ`, and pydantic reads `os.environ`
whatever `Settings.model_config["env_file"]` has been set to. So the suite's
`VIGIL_DISABLE_DOTENV` closed one way in and left this one open, and from the
first such import every `get_settings()` answered from the developer's file.

Both directions are pinned here. Losing the guard turns the first pair red;
letting the guard swallow the spawned case turns the second pair red, which
would be the quieter regression -- a server that cannot find its credentials
reports "not configured" rather than failing.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MARKER = "VIGIL_DOTENV_ISOLATION_MARKER"
TOOL_MODULES = [
    "core.integrations.splunk.tool",
    "core.integrations.vstrike.tool",
]


def _marker_after_import(module: str, tmp_path: Path, disable_dotenv: bool) -> str:
    """Import `module` in a fresh interpreter whose cwd holds a one-line .env."""
    (tmp_path / ".env").write_text(f"{MARKER}=leaked\n")

    script = textwrap.dedent(f"""
        import importlib
        import os

        importlib.import_module({module!r})
        print(os.environ.get({MARKER!r}, ""))
        """)

    env = {k: v for k, v in os.environ.items() if k != MARKER}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if disable_dotenv:
        env["VIGIL_DISABLE_DOTENV"] = "1"
    else:
        env.pop("VIGIL_DISABLE_DOTENV", None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"importing {module} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_importing_it_does_not_copy_dotenv_into_the_environment(module, tmp_path):
    """What the test suite does: VIGIL_DISABLE_DOTENV is set, so nothing leaks."""
    assert _marker_after_import(module, tmp_path, disable_dotenv=True) == ""


@pytest.mark.parametrize("module", TOOL_MODULES)
def test_running_it_as_a_server_still_reads_dotenv(module, tmp_path):
    """What the spawned server does: no flag, and `.env` is how it is configured."""
    assert _marker_after_import(module, tmp_path, disable_dotenv=False) == "leaked"
