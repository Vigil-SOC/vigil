"""Regression (#1109): ingestion clients must get secrets saved from Settings.

``split_secrets`` strips secret fields before the config is persisted, so a
client built from ``get_integration_config`` ran with an empty credential. The
stored configs below omit every secret field, exactly as production leaves
them; only the seeded secrets store holds the credential.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import core.integrations._base.config as resolver
from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion
from core.integrations.crowdstrike.adapter import CrowdStrikeAdapter
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion
from core.integrations.splunk.adapter import SplunkAdapter

pytestmark = pytest.mark.unit

_STORED = {
    "splunk": {"server_url": "https://splunk:8089", "username": "svc"},
    "crowdstrike": {"client_id": "cs-id"},
    "aws-security-hub": {"access_key_id": "AKIA-test"},
    "microsoft-defender": {"tenant_id": "tenant", "client_id": "md-id"},
}
_SECRETS = {
    "SPLUNK_PASSWORD": "splunk-pw",
    "FALCON_CLIENT_SECRET": "cs-secret",
    "AWS_SECURITY_HUB_SECRET_ACCESS_KEY": "aws-secret",
    "MICROSOFT_DEFENDER_CLIENT_SECRET": "md-secret",
}


@pytest.fixture(autouse=True)
def seeded(monkeypatch):
    monkeypatch.setattr(
        resolver, "get_integration_config", lambda i: dict(_STORED.get(i, {}))
    )
    monkeypatch.setattr(
        resolver, "get_secret", lambda key, default=None: _SECRETS.get(key, default)
    )


def _make_poller():
    from services.daemon.config import PollingConfig
    from services.daemon.poller import DataPoller

    with (
        patch("services.daemon.poller.FederationRunner"),
        patch("services.daemon.poller.RedisDedupSet"),
    ):
        return DataPoller(PollingConfig())


def _init_poller(enabled: set[str]):
    db = types.ModuleType("core.storage.database_data_service")
    db.DatabaseDataService = MagicMock()
    with (
        patch.dict(sys.modules, {"core.storage.database_data_service": db}),
        patch("core.config.is_integration_enabled", side_effect=enabled.__contains__),
        patch("core.integrations.splunk.client.SplunkService") as splunk,
        patch("core.integrations.crowdstrike.client.CrowdStrikeService") as cs,
    ):
        _make_poller()._init_services()
    return splunk, cs


def test_splunk_password_reaches_service_from_poller():
    splunk, _ = _init_poller({"splunk"})
    splunk.assert_called_once_with(
        server_url="https://splunk:8089",
        username="svc",
        password="splunk-pw",
        verify_ssl=False,
    )


def test_splunk_password_reaches_service_from_adapter():
    adapter = SplunkAdapter()
    with (
        patch.object(adapter, "is_configured", return_value=True),
        patch("core.integrations.splunk.client.SplunkService") as splunk,
    ):
        adapter._get_service()
    splunk.assert_called_once_with(
        server_url="https://splunk:8089",
        username="svc",
        password="splunk-pw",
        verify_ssl=False,
    )


def test_crowdstrike_secret_reaches_service_from_poller():
    _, cs = _init_poller({"crowdstrike"})
    cs.assert_called_once_with(
        client_id="cs-id",
        client_secret="cs-secret",
        base_url="https://api.crowdstrike.com",
    )


def test_crowdstrike_secret_reaches_service_from_adapter():
    adapter = CrowdStrikeAdapter()
    with (
        patch.object(adapter, "is_configured", return_value=True),
        patch("core.integrations.crowdstrike.client.CrowdStrikeService") as cs,
    ):
        adapter._get_service()
    cs.assert_called_once_with(
        client_id="cs-id",
        client_secret="cs-secret",
        base_url="https://api.crowdstrike.com",
    )


def test_defender_ingestion_config_has_client_secret():
    assert MicrosoftDefenderIngestion().config["client_secret"] == "md-secret"


def test_security_hub_ingestion_config_has_secret_access_key():
    cfg = AWSSecurityHubIngestion().config
    assert cfg["access_key_id"] == "AKIA-test"
    assert cfg["secret_access_key"] == "aws-secret"
