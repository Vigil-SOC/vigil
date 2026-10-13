"""One answer to "is this integration set up", read by every readiness surface.

splunk-selfhosted is on by default and reads its settings through resolve(), so
with nothing saved it still starts and answers every call "Splunk not
configured". Settings rated it Good, chat named it as connected and a hunt
counted it as log search. Each now reads ``is_configured``.
"""

from __future__ import annotations

import pytest

import core.integrations._base.config as resolver
from core.integrations._base.config import is_configured, server_configured
from core.integrations.mcp.service import MCPService
from core.integrations.splunk.descriptor import SPLUNK
from core.integrations.virustotal.descriptor import VIRUSTOTAL

pytestmark = pytest.mark.unit

SPLUNK_FORM = {"server_url": "https://splunk.example.com:8089", "username": "svc"}


@pytest.fixture
def saved(monkeypatch):
    """The Settings rows and the secrets store (env included), empty to start."""
    stored: dict = {}
    secrets: dict = {}
    monkeypatch.setattr(
        resolver, "get_integration_config", lambda i: dict(stored.get(i, {}))
    )
    monkeypatch.setattr(
        resolver, "get_secret", lambda key, default=None: secrets.get(key, default)
    )
    return stored, secrets


def test_nothing_saved_is_not_configured(saved):
    assert is_configured(SPLUNK) is False
    assert server_configured("splunk-selfhosted") is False


def test_the_login_env_example_ships_is_not_configured_without_a_url(saved):
    # env.example sets these two and leaves SPLUNK_URL commented out.
    _, secrets = saved
    secrets.update({"SPLUNK_USERNAME": "admin", "SPLUNK_PASSWORD": "changeme"})
    assert is_configured(SPLUNK) is False


def test_the_settings_form_configures_it(saved):
    stored, secrets = saved
    stored["splunk"] = dict(SPLUNK_FORM)
    secrets["SPLUNK_PASSWORD"] = "pw"
    assert is_configured(SPLUNK) is True


def test_the_env_alone_configures_it(saved):
    _, secrets = saved
    secrets.update(
        {
            "SPLUNK_URL": SPLUNK_FORM["server_url"],
            "SPLUNK_USERNAME": "svc",
            "SPLUNK_PASSWORD": "pw",
        }
    )
    assert is_configured(SPLUNK) is True


def test_a_descriptor_that_requires_nothing_needs_nothing_saved(saved):
    assert VIRUSTOTAL.required_fields == ()
    assert is_configured(VIRUSTOTAL) is True


def test_a_server_no_descriptor_names_is_configured(saved):
    assert server_configured("security-detections") is True


def test_settings_that_cannot_be_read_are_not_configured(monkeypatch, caplog):
    def _boom(_id):
        raise RuntimeError("secret-looking text")

    monkeypatch.setattr(resolver, "get_integration_config", _boom)
    assert is_configured(SPLUNK) is False
    assert "RuntimeError" in caplog.text
    assert "secret-looking text" not in caplog.text


class TestTheCatalog:
    def test_the_self_hosted_server_waits_for_the_splunk_form(self, saved):
        service = MCPService()
        assert service.is_configured("splunk-selfhosted") is False

        stored, secrets = saved
        stored["splunk"] = dict(SPLUNK_FORM)
        secrets["SPLUNK_PASSWORD"] = "pw"
        assert service.is_configured("splunk-selfhosted") is True

    def test_a_placeholder_server_is_not_held_to_the_form(self, saved):
        # The official Splunk server shares the descriptor but reads
        # SPLUNK_MCP_URL, and is held dormant until that resolves.
        service = MCPService()
        assert service.servers["splunk"].required_env_vars == ["SPLUNK_MCP_URL"]
        assert service.is_configured("splunk") is True
