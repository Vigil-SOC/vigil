"""Regression (#1109): ingestion clients must get secrets saved from Settings.

``split_secrets`` strips secret fields before the config is persisted, so a
client built from ``get_integration_config`` ran with an empty credential. The
stored configs below omit every secret field, exactly as production leaves
them; only the seeded secrets store holds the credential.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import core.integrations._base.config as resolver
from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion
from core.integrations.crowdstrike.adapter import CrowdStrikeAdapter
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion
from core.integrations.splunk.adapter import SplunkAdapter

pytestmark = pytest.mark.unit

_STORED = {
    "splunk": {"server_url": "https://splunk:8089", "username": "svc"},
    "crowdstrike": {"client_id": "cs-id"},
    "aws-security-hub": {"access_key_id": "AKIA-test"},
    "microsoft-defender": {"tenant_id": "tenant", "client_id": "md-id"},
    "azure-sentinel": {
        "tenant_id": "tenant",
        "client_id": "client",
        "subscription_id": "sub",
        "resource_group": "rg",
        "workspace_name": "ws",
    },
}
_SECRETS = {
    "SPLUNK_PASSWORD": "splunk-pw",
    "FALCON_CLIENT_SECRET": "cs-secret",
    "AWS_SECURITY_HUB_SECRET_ACCESS_KEY": "aws-secret",
    "MICROSOFT_DEFENDER_CLIENT_SECRET": "md-secret",
    "AZURE_SENTINEL_CLIENT_SECRET": "sentinel-secret",
}


@pytest.fixture(autouse=True)
def seeded(monkeypatch):
    monkeypatch.setattr(
        resolver, "get_integration_config", lambda i: dict(_STORED.get(i, {}))
    )
    monkeypatch.setattr(
        resolver, "get_secret", lambda key, default=None: _SECRETS.get(key, default)
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
        ca_cert_path=None,
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


def test_sentinel_ingestion_resolves_secret_and_workspace_fields():
    cfg = AzureSentinelIngestion().config
    assert cfg == {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "sentinel-secret",
        "subscription_id": "sub",
        "resource_group": "rg",
        "workspace_name": "ws",
    }


def test_security_hub_ingestion_config_has_secret_access_key():
    cfg = AWSSecurityHubIngestion().config
    assert cfg["access_key_id"] == "AKIA-test"
    assert cfg["secret_access_key"] == "aws-secret"
