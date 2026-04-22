"""Regression tests for the Splunk MCP secrets migration (GH #84 PR-F follow-up).

``tools/_legacy/splunk.py`` previously read SPLUNK_* credentials straight
from ``os.environ``. It now routes through ``backend/secrets_manager`` so
operators can keep Splunk creds in the keyring / dotenv without surfacing
them in ``.env``. These tests lock in the fallback behavior so a refactor
can't silently revert to plain env reads.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent.parent
SPLUNK_TOOL = REPO / "tools" / "_legacy" / "splunk.py"


@pytest.fixture
def splunk_mod(monkeypatch):
    """Import the Splunk MCP tool in isolation, with SPLUNK_* env cleared.

    Note: the module calls ``load_dotenv()`` at import time, so env vars
    have to be cleared *after* exec_module runs — dotenv would otherwise
    repopulate them from the project ``.env`` file.
    """
    spec = importlib.util.spec_from_file_location("splunk_tool_under_test", SPLUNK_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in ("SPLUNK_URL", "SPLUNK_USERNAME", "SPLUNK_PASSWORD", "SPLUNK_VERIFY_SSL"):
        monkeypatch.delenv(key, raising=False)
    return module


def test_env_read_via_secrets_manager(splunk_mod, monkeypatch):
    """Plain env vars still resolve — ``secrets_manager`` checks env first."""
    monkeypatch.setenv("SPLUNK_URL", "https://from-env.example:8089")
    monkeypatch.setenv("SPLUNK_USERNAME", "env-admin")
    assert splunk_mod._read_credential("SPLUNK_URL") == "https://from-env.example:8089"
    assert splunk_mod._read_credential("SPLUNK_USERNAME") == "env-admin"


def test_returns_default_when_unset(splunk_mod):
    assert splunk_mod._read_credential("SPLUNK_URL") is None
    assert splunk_mod._read_credential("SPLUNK_URL", "http://fallback") == "http://fallback"


def test_secrets_manager_wins_over_env(splunk_mod, monkeypatch):
    """When secrets_manager returns a value it takes precedence over the env
    fallback — the env read only fires if the secrets-manager chain is
    empty or the import failed."""
    monkeypatch.setenv("SPLUNK_PASSWORD", "from-env")

    def fake_get_secret(key):
        if key == "SPLUNK_PASSWORD":
            return "from-keyring"
        return None

    monkeypatch.setattr(splunk_mod, "_get_secret", fake_get_secret)
    assert splunk_mod._read_credential("SPLUNK_PASSWORD") == "from-keyring"


def test_graceful_fallback_when_secrets_manager_unavailable(splunk_mod, monkeypatch):
    """Subprocess startup outside the repo may not be able to import
    secrets_manager. The tool should still read env vars in that case."""
    monkeypatch.setenv("SPLUNK_URL", "https://env-only.example:8089")
    monkeypatch.setattr(splunk_mod, "_get_secret", None)
    assert splunk_mod._read_credential("SPLUNK_URL") == "https://env-only.example:8089"


def test_no_direct_environ_reads_for_splunk_creds():
    """Guardrail: the migrated file must not regress to ``os.environ.get``
    for SPLUNK_* credentials. Only ``_read_credential`` should touch them.
    """
    text = SPLUNK_TOOL.read_text()
    # These patterns indicate legacy direct env reads for credentials.
    forbidden = [
        'os.environ.get("SPLUNK_URL"',
        'os.environ.get("SPLUNK_USERNAME"',
        'os.environ.get("SPLUNK_PASSWORD"',
        'os.environ.get("SPLUNK_VERIFY_SSL"',
    ]
    for pattern in forbidden:
        assert pattern not in text, f"Legacy env read resurfaced: {pattern}"
