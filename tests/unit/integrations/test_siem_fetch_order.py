"""Federation's fetch asks each SIEM for the oldest alerts first (#1233).

A batch that fills ``limit`` is only a safe place to stop the cursor when it
is a contiguous oldest-first prefix of the window. ``oldest_first=True`` is
what ``SIEMIngestionAdapter`` passes; the default call, which the daemon
poller makes, keeps each service's existing order.
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# -- Elastic ---------------------------------------------------------------


def _elastic():
    from core.integrations.elastic.ingestion import ElasticIngestion

    with patch(
        "core.integrations.elastic.ingestion.resolve",
        return_value={
            "elasticsearch_url": "https://es.test:9200",
            "kibana_url": "https://kb.test:5601",
        },
    ):
        svc = ElasticIngestion()
    svc._elastic_service = MagicMock(
        kibana_url="https://kb.test:5601",
        fetch_detection_alerts=AsyncMock(return_value={"hits": {"hits": []}}),
    )
    return svc


@pytest.mark.asyncio
async def test_elastic_default_call_sorts_newest_first():
    svc = _elastic()
    await svc.fetch_alerts()
    kwargs = svc._elastic_service.fetch_detection_alerts.call_args.kwargs
    assert kwargs["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_elastic_federation_call_sorts_oldest_first():
    svc = _elastic()
    await svc.fetch_alerts(oldest_first=True, limit=7)
    kwargs = svc._elastic_service.fetch_detection_alerts.call_args.kwargs
    assert kwargs["sort_order"] == "asc"
    assert kwargs["size"] == 7


# -- Microsoft Defender ----------------------------------------------------


def _defender():
    from core.integrations.microsoft_defender.ingestion import (
        MicrosoftDefenderIngestion,
    )

    with patch(
        "core.integrations.microsoft_defender.ingestion.resolve",
        return_value={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    ):
        return MicrosoftDefenderIngestion()


async def _defender_params(**kwargs):
    svc = _defender()
    response = MagicMock()
    response.json.return_value = {"value": []}
    with patch.object(svc, "_get_access_token", return_value="token"), patch(
        "core.integrations.microsoft_defender.ingestion.httpx.get",
        return_value=response,
    ) as get:
        await svc.fetch_alerts(**kwargs)
    return get.call_args.kwargs["params"]


@pytest.mark.asyncio
async def test_defender_default_call_orders_newest_first():
    params = await _defender_params()
    assert params["$orderby"] == "alertCreationTime desc"


@pytest.mark.asyncio
async def test_defender_federation_call_orders_oldest_first():
    params = await _defender_params(oldest_first=True, limit=7)
    assert params["$orderby"] == "alertCreationTime asc"
    assert params["$top"] == 7


# -- AWS Security Hub ------------------------------------------------------


async def _security_hub_paginate_kwargs(**kwargs):
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
        svc = AWSSecurityHubIngestion()
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"Findings": []}]
    with patch("boto3.client", return_value=client):
        await svc.fetch_alerts(**kwargs)
    return client.get_paginator.return_value.paginate.call_args.kwargs


@pytest.mark.asyncio
async def test_security_hub_default_call_keeps_api_order():
    kwargs = await _security_hub_paginate_kwargs()
    assert "SortCriteria" not in kwargs
    assert kwargs["MaxResults"] == 100


@pytest.mark.asyncio
async def test_security_hub_federation_call_sorts_by_created_at_ascending():
    kwargs = await _security_hub_paginate_kwargs(oldest_first=True, limit=7)
    assert kwargs["SortCriteria"] == [{"Field": "CreatedAt", "SortOrder": "asc"}]
    assert kwargs["MaxResults"] == 7


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


def _sentinel():
    from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

    with patch(
        "core.integrations.azure_sentinel.ingestion.resolve",
        return_value={
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "subscription_id": "sub",
            "resource_group": "rg",
            "workspace_name": "ws",
        },
    ):
        return AzureSentinelIngestion()


def _incident(name, created):
    data = types.SimpleNamespace(alerts_count=1, tactics=[])
    return types.SimpleNamespace(
        name=name,
        title="Incident",
        description="",
        severity="Medium",
        status="New",
        created_time_utc=created,
        last_modified_time_utc=None,
        owner=None,
        labels=[],
        additional_data=data,
    )


def _unordered_incidents(fake_azure_sdk):
    """Three in-window incidents in the API's arbitrary order: mid, newest, oldest."""
    now = datetime.now(timezone.utc)
    fake_azure_sdk.SecurityInsights.return_value.incidents.list.return_value = [
        _incident("mid", now - timedelta(hours=2)),
        _incident("newest", now - timedelta(hours=1)),
        _incident("oldest", now - timedelta(hours=3)),
    ]


@pytest.mark.asyncio
async def test_sentinel_default_call_stops_at_limit_in_api_order(fake_azure_sdk):
    _unordered_incidents(fake_azure_sdk)
    incidents = await _sentinel().fetch_alerts(limit=2)
    assert [i["id"] for i in incidents] == ["mid", "newest"]


@pytest.mark.asyncio
async def test_sentinel_federation_call_scans_the_window_then_takes_the_oldest(
    fake_azure_sdk,
):
    _unordered_incidents(fake_azure_sdk)
    incidents = await _sentinel().fetch_alerts(limit=2, oldest_first=True)
    assert [i["id"] for i in incidents] == ["oldest", "mid"]


@pytest.mark.asyncio
async def test_sentinel_undated_incident_sorts_last_and_does_not_take_a_slot(
    fake_azure_sdk,
):
    """No created time: the window filter lets it through; it must not lead the batch."""
    now = datetime.now(timezone.utc)
    fake_azure_sdk.SecurityInsights.return_value.incidents.list.return_value = [
        _incident("undated", None),
        _incident("newest", now - timedelta(hours=1)),
        _incident("oldest", now - timedelta(hours=3)),
    ]
    incidents = await _sentinel().fetch_alerts(limit=2, oldest_first=True)
    assert [i["id"] for i in incidents] == ["oldest", "newest"]
    everything = await _sentinel().fetch_alerts(limit=10, oldest_first=True)
    assert [i["id"] for i in everything] == ["oldest", "newest", "undated"]
