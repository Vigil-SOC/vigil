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


# --- The other file that is also a .env --------------------------------------
#
# `core/secrets_manager.py`'s DotEnvBackend reads the state directory's own
# `.env`, not the repo's, and never writes to `os.environ` -- so it does not
# make `get_settings()` answer differently. What it does is hand a test the
# operator's real credential when it asks for one, which is the same leak in a
# different coat: the acceptance is that every path that loads a `.env` asks
# the same flag.
#
# Only the file it reaches for on its own. A caller that names a file has said
# which one it means -- `migrate_dotenv_secrets_to_encrypted()` reloads a named
# file on purpose -- and guarding that too would break the migration rather
# than close anything.


def _state_dir_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    dotenv = tmp_path / ".env"
    dotenv.write_text("SPLUNK_PASSWORD=the-operators-real-one\n")
    return dotenv


def test_the_secrets_backend_does_not_read_the_state_directorys_dotenv(
    tmp_path, monkeypatch
):
    from core.secrets_manager import DotEnvBackend

    _state_dir_dotenv(tmp_path, monkeypatch)
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")

    assert DotEnvBackend().get("SPLUNK_PASSWORD") is None, (
        "A test asking for a credential was handed the value from the "
        "operator's .env. Every path that loads one has to honour "
        "VIGIL_DISABLE_DOTENV, not only the ones that write into os.environ."
    )


def test_the_secrets_backend_still_reads_it_outside_a_test_run(tmp_path, monkeypatch):
    """The other direction: this file is where an operator's secrets live."""
    from core.secrets_manager import DotEnvBackend

    _state_dir_dotenv(tmp_path, monkeypatch)
    monkeypatch.delenv("VIGIL_DISABLE_DOTENV", raising=False)

    assert DotEnvBackend().get("SPLUNK_PASSWORD") == "the-operators-real-one"


def test_a_file_the_caller_named_is_read_either_way(tmp_path, monkeypatch):
    """Naming it is the asking -- the migration reloads a named file on purpose."""
    from core.secrets_manager import DotEnvBackend

    named = tmp_path / "named.env"
    named.write_text("SPLUNK_PASSWORD=one-the-caller-asked-for\n")
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")

    assert DotEnvBackend(named).get("SPLUNK_PASSWORD") == "one-the-caller-asked-for"
