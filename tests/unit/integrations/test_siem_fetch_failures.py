"""A SIEM fetch that fails must raise, so federation keeps its cursor (#1214).

Returning [] made a failed poll look like a successful empty one: the runner
recorded success and advanced the cursor past the outage. Sentinel, Defender
and Elastic still return [] when their configuration is incomplete. Security
Hub has no such check: it falls back to boto3's default credential chain, and
an enabled source that finds no credentials raises like any other outage.
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


@pytest.mark.asyncio
async def test_sentinel_compares_aware_incident_times(fake_azure_sdk):
    """The SDK returns aware datetimes; the window is naive UTC."""
    from datetime import datetime, timedelta, timezone

    def incident(name, created):
        i = MagicMock()
        i.name = name
        i.created_time_utc = created
        i.last_updated_time_utc = None
        i.owner = None
        i.labels = []
        i.additional_data = None
        i.additional_properties = {}
        return i

    now = datetime.now(timezone.utc)
    fake_azure_sdk.SecurityInsights.return_value.incidents.list.return_value = [
        incident("recent", now - timedelta(hours=1)),
        incident("old", now - timedelta(days=3)),
    ]
    incidents = await _sentinel(_SENTINEL_CONFIG).fetch_alerts()
    assert [i["id"] for i in incidents] == ["recent"]


@pytest.mark.asyncio
async def test_elastic_without_kibana_is_not_a_failure():
    from core.integrations.elastic.ingestion import ElasticIngestion

    with patch(
        "core.integrations.elastic.ingestion.resolve",
        return_value={"elasticsearch_url": "https://es.test:9200", "kibana_url": None},
    ):
        assert await ElasticIngestion().fetch_alerts() == []
