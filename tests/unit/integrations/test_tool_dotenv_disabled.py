"""The vendor tool servers must honour VIGIL_DISABLE_DOTENV (GH #974).

``core/integrations/<vendor>/tool.py`` call ``load_dotenv()`` at import so that,
spawned as a standalone server, they read the project ``.env``. Imported into
the test suite instead, that call would write a developer's whole ``.env`` into
``os.environ`` -- which pydantic reads regardless of ``Settings.env_file`` -- and
every later ``get_settings()`` would answer from the developer's configuration.

``load_dotenv()`` resolves the file by walking up from the *calling module's*
directory, not the cwd, so a tmp ``.env`` cannot stand in for the repo root one.
Intercept the call itself instead: each module is exec'd fresh from its path
(not reloaded from sys.modules) and the recorder shows whether it fired.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import dotenv
import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
TOOL_MODULES = [
    pytest.param(REPO / "core" / "integrations" / vendor / "tool.py", id=vendor)
    for vendor in ("vstrike", "splunk")
]


def _exec_fresh(path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        f"{path.parent.name}_tool_fresh", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


@pytest.fixture
def load_dotenv_calls(monkeypatch):
    calls: list[tuple] = []
    # Patch at the source: the tool modules do ``from dotenv import load_dotenv``
    # at import, so a fresh exec picks up the recorder.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: calls.append(a) or True)
    return calls


@pytest.mark.parametrize("tool_path", TOOL_MODULES)
def test_import_under_suite_does_not_load_dotenv(tool_path, load_dotenv_calls):
    # tests/conftest.py sets this before collection; assert rather than assume.
    assert os.environ.get("VIGIL_DISABLE_DOTENV")
    _exec_fresh(tool_path)
    assert load_dotenv_calls == []


@pytest.mark.parametrize("tool_path", TOOL_MODULES)
def test_spawned_server_still_loads_dotenv(tool_path, load_dotenv_calls, monkeypatch):
    # A spawned ``python3 core/integrations/<vendor>/tool.py`` runs with a
    # narrowed env where the flag is absent; .env is its config source.
    monkeypatch.delenv("VIGIL_DISABLE_DOTENV")
    _exec_fresh(tool_path)
    assert len(load_dotenv_calls) == 1
