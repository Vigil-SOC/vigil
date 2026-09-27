"""A SIEM fetch that fails must raise, so federation keeps its cursor (#1214).

Returning [] made a failed poll look like a successful empty one: the runner
recorded success and advanced the cursor past the outage. Missing
configuration is not a failure and still returns [].
"""

import sys
import types
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.unit


# -- Azure Sentinel --------------------------------------------------------


@pytest.fixture
def fake_azure_sdk(monkeypatch):
    """The Azure SDK is optional; stub the two modules fetch_alerts imports."""
    identity = types.ModuleType("azure.identity")
    identity.ClientSecretCredential = MagicMock()
    insights = types.ModuleType("azure.mgmt.securityinsight")
    insights.SecurityInsights = MagicMock()
    for name, module in {
        "azure": types.ModuleType("azure"),
        "azure.identity": identity,
        "azure.mgmt": types.ModuleType("azure.mgmt"),
        "azure.mgmt.securityinsight": insights,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return insights


def _sentinel(config):
    from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

    with patch(
        "core.integrations.azure_sentinel.ingestion.get_integration_config",
        return_value=config,
    ):
        return AzureSentinelIngestion()


_SENTINEL_CONFIG = {
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "s",
    "subscription_id": "sub",
    "resource_group": "rg",
    "workspace_name": "ws",
}


@pytest.mark.asyncio
async def test_sentinel_api_failure_raises(fake_azure_sdk):
    fake_azure_sdk.SecurityInsights.return_value.incidents.list.side_effect = (
        RuntimeError("workspace unreachable")
    )
    with pytest.raises(RuntimeError, match="workspace unreachable"):
        await _sentinel(_SENTINEL_CONFIG).fetch_alerts()


@pytest.mark.asyncio
async def test_sentinel_incomplete_config_is_not_a_failure(fake_azure_sdk):
    assert await _sentinel({"tenant_id": "t"}).fetch_alerts() == []


# -- AWS Security Hub ------------------------------------------------------


def _security_hub():
    from core.integrations.aws_security_hub.ingestion import (
        AWSSecurityHubIngestion,
    )

    with patch(
        "core.integrations.aws_security_hub.ingestion.resolve",
        return_value={
            "region": "us-east-1",
            "access_key_id": "a",
            "secret_access_key": "b",
        },
    ):
        return AWSSecurityHubIngestion()


@pytest.mark.asyncio
async def test_security_hub_client_error_raises():
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetFindings"
    )
    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = error
    with patch("boto3.client", return_value=client):
        with pytest.raises(ClientError):
            await _security_hub().fetch_alerts()


# -- Microsoft Defender ----------------------------------------------------


def _defender(config):
    from core.integrations.microsoft_defender.ingestion import (
        MicrosoftDefenderIngestion,
    )

    with patch(
        "core.integrations.microsoft_defender.ingestion.resolve",
        return_value=config,
    ):
        return MicrosoftDefenderIngestion()


_DEFENDER_CONFIG = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}


@pytest.mark.asyncio
async def test_defender_token_failure_raises():
    with patch(
        "core.integrations.microsoft_defender.ingestion.httpx.post",
        side_effect=httpx.ConnectError("login unreachable"),
    ):
        with pytest.raises(httpx.ConnectError):
            await _defender(_DEFENDER_CONFIG).fetch_alerts()


@pytest.mark.asyncio
async def test_defender_api_failure_raises():
    request = httpx.Request(
        "GET", "https://api.securitycenter.microsoft.com/api/alerts"
    )
    denied = httpx.Response(403, request=request)
    svc = _defender(_DEFENDER_CONFIG)
    with patch.object(svc, "_get_access_token", return_value="token"), patch(
        "core.integrations.microsoft_defender.ingestion.httpx.get",
        return_value=denied,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await svc.fetch_alerts()


@pytest.mark.asyncio
async def test_defender_incomplete_config_is_not_a_failure():
    assert await _defender({}).fetch_alerts() == []
