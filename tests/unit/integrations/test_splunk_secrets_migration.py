"""The self-hosted Splunk MCP tool reads its config through the descriptor resolver.

``core/integrations/splunk/tool.py`` used to read SPLUNK_URL, SPLUNK_USERNAME and
SPLUNK_PASSWORD through the secrets store and then the environment. ``server_url``
and ``username`` are not secrets: Settings writes them onto the integration row,
where that read never looked, so every hunt reported "Splunk not configured" no
matter what the operator saved (#1113). These tests lock the two paths that must
both construct the client: a Settings save with no SPLUNK_* in the environment,
and an env-only deployment with nothing saved.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

import core.integrations._base.config as resolver
import core.integrations.splunk.client as splunk_client

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent.parent.parent
SPLUNK_TOOL = REPO / "core" / "integrations" / "splunk" / "tool.py"

_SPLUNK_ENV = ("SPLUNK_URL", "SPLUNK_USERNAME", "SPLUNK_PASSWORD", "SPLUNK_VERIFY_SSL")


class _Constructed:
    """Stand-in for SplunkService that records what it was built with."""

    def __init__(self, server_url, username, password, verify_ssl=False):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl


@pytest.fixture
def splunk_mod(monkeypatch):
    """Import the Splunk MCP tool in isolation, with SPLUNK_* env cleared.

    The module's import-time ``load_dotenv()`` is skipped under the suite's
    ``VIGIL_DISABLE_DOTENV`` (tests/conftest.py), so a developer's ``.env``
    cannot repopulate these keys; see test_tool_dotenv_disabled.py.
    """
    spec = importlib.util.spec_from_file_location("splunk_tool_under_test", SPLUNK_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in _SPLUNK_ENV:
        monkeypatch.delenv(key, raising=False)
    # The tool imports SplunkService lazily from the client module, so patching
    # it there is what get_splunk_service() sees.
    monkeypatch.setattr(splunk_client, "SplunkService", _Constructed)
    return module


def _seed(monkeypatch, stored, secrets):
    """Seed the resolver the way production writes: non-secrets on the
    integration row, secrets in the store, env consulted only through
    get_secret. Follows tests/unit/integrations/test_config_resolver.py."""
    monkeypatch.setattr(resolver, "get_integration_config", lambda _id: dict(stored))
    monkeypatch.setattr(
        resolver,
        "get_secret",
        lambda key, default=None: secrets.get(key, os.environ.get(key, default)),
    )


def test_settings_saved_config_reaches_the_client(splunk_mod, monkeypatch):
    """A URL and username saved in Settings, plus the password the save routed
    to the secrets store, are what the client is constructed with -- with no
    SPLUNK_* in the environment."""
    _seed(
        monkeypatch,
        stored={
            "server_url": "https://stored.example:8089",
            "username": "stored-admin",
            "verify_ssl": "true",
        },
        secrets={"SPLUNK_PASSWORD": "from-secrets-store"},
    )

    service = splunk_mod.get_splunk_service()

    assert service is not None, "Settings-saved Splunk read as not configured"
    assert service.server_url == "https://stored.example:8089"
    assert service.username == "stored-admin"
    assert service.password == "from-secrets-store"
    assert service.verify_ssl is True


def test_env_only_deployment_still_constructs_the_client(splunk_mod, monkeypatch):
    """Nothing saved in Settings: SPLUNK_URL, SPLUNK_USERNAME and SPLUNK_PASSWORD
    from the environment still build the client. SPLUNK_URL, not the canonical
    SPLUNK_SERVER_URL, because that is the name every deployment already sets."""
    monkeypatch.setenv("SPLUNK_URL", "https://env-only.example:8089")
    monkeypatch.setenv("SPLUNK_USERNAME", "env-admin")
    monkeypatch.setenv("SPLUNK_PASSWORD", "env-password")
    _seed(monkeypatch, stored={}, secrets={})

    service = splunk_mod.get_splunk_service()

    assert service is not None, "env-only Splunk read as not configured"
    assert service.server_url == "https://env-only.example:8089"
    assert service.username == "env-admin"
    assert service.password == "env-password"


def test_unset_verify_ssl_means_no_verification(splunk_mod, monkeypatch):
    """resolve() hands back None for an unset verify_ssl. Unset has always meant
    no verification for the self-hosted server (port 8089 ships a self-signed
    certificate), so None must stay False rather than default to True."""
    _seed(
        monkeypatch,
        stored={"server_url": "https://stored.example:8089", "username": "admin"},
        secrets={"SPLUNK_PASSWORD": "secret"},
    )

    service = splunk_mod.get_splunk_service()

    assert service is not None
    assert service.verify_ssl is False


def test_no_server_url_anywhere_is_not_configured(splunk_mod, monkeypatch):
    _seed(monkeypatch, stored={"username": "admin"}, secrets={"SPLUNK_PASSWORD": "x"})

    assert splunk_mod.get_splunk_service() is None


def test_no_direct_environ_reads_for_splunk_creds():
    """Guardrail: the tool must not regress to reading SPLUNK_* itself, from
    the environment or the secrets store. ``resolve`` is the one path."""
    text = SPLUNK_TOOL.read_text()
    forbidden = [
        'os.environ.get("SPLUNK_',
        'get_secret("SPLUNK_',
        "_read_credential(",
    ]
    for pattern in forbidden:
        assert pattern not in text, f"Direct SPLUNK_* read resurfaced: {pattern}"
