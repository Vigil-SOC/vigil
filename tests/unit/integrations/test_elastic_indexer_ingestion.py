"""Elastic ingestion without Kibana reads a Wazuh indexer's alert index (#1252).

With the Kibana URL blank, ``ElasticIngestion`` searches the configured index
pattern for alerts at or above ``min_rule_level``, oldest first, and maps the
Wazuh schema onto a finding. The window itself is the adapter's business.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from core.integrations.elastic.ingestion import (
    DEFAULT_MIN_RULE_LEVEL,
    ElasticIngestion,
    is_wazuh_alert,
    min_rule_level_from_config,
    severity_for_rule_level,
)

pytestmark = pytest.mark.unit

ES_URL = "https://indexer.test:9200"
INDEX = "wazuh-alerts-4.x-*"
SEARCH = f"{ES_URL}/{INDEX}/_search"
NOW = datetime(2026, 9, 28, 12, 0, 0)

# Shape of a wazuh-alerts-4.x document (Wazuh 4.14) after its Filebeat pipeline.
ALERT = {
    "_index": "wazuh-alerts-4.x-2026.09.28",
    "_id": "abc123",
    "_source": {
        "timestamp": "2026-09-28T06:17:18.394-0500",
        "@timestamp": "2026-09-28T11:17:18.394Z",
        "id": "1790597838.2112422",
        "agent": {"id": "001", "name": "web01", "ip": "10.0.0.5"},
        "manager": {"name": "wazuh-manager"},
        "rule": {
            "id": "5712",
            "level": 10,
            "description": "sshd: brute force trying to get access to the system.",
            "groups": ["syslog", "sshd", "authentication_failures"],
            "mitre": {
                "id": ["T1110"],
                "tactic": ["Credential Access"],
                "technique": ["Brute Force"],
            },
        },
        "data": {
            "srcip": "203.0.113.7",
            "dstuser": "root",
            "win": {"eventdata": {"targetUserName": "svc-backup"}},
        },
        "full_log": "Sep 28 11:17:17 web01 sshd[1]: Failed password for root",
    },
}


def _ingestion(**overrides) -> ElasticIngestion:
    config = {
        "elasticsearch_url": ES_URL,
        "kibana_url": None,
        "api_key": None,
        "username": "vigil-reader",
        "password": "secret",
        "index_pattern": INDEX,
        "min_rule_level": None,
        "verify_ssl": False,
    }
    config.update(overrides)
    with patch("core.integrations.elastic.ingestion.resolve", return_value=config):
        svc = ElasticIngestion()
    svc.ingestion_service = MagicMock()
    return svc


def _range(body: dict, field: str) -> dict:
    for clause in body["query"]["bool"]["filter"]:
        if field in clause.get("range", {}):
            return clause["range"][field]
    raise AssertionError(f"no range filter on {field}: {body}")


@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, DEFAULT_MIN_RULE_LEVEL),
        (0, DEFAULT_MIN_RULE_LEVEL),
        ("", DEFAULT_MIN_RULE_LEVEL),
        (12, 12),
        ("12", 12),
        (99, 16),
        (-3, 1),
        ("junk", DEFAULT_MIN_RULE_LEVEL),
    ],
)
def test_min_rule_level_from_config(stored, expected):
    assert min_rule_level_from_config({"min_rule_level": stored}) == expected


@respx.mock
@pytest.mark.asyncio
async def test_fetch_without_kibana_searches_the_index_oldest_first():
    route = respx.post(SEARCH).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [ALERT]}})
    )
    ingestion = _ingestion(min_rule_level=12)
    start, end = NOW - timedelta(minutes=5), NOW - timedelta(minutes=1)

    hits = await ingestion.fetch_alerts(
        start_time=start, end_time=end, limit=50, oldest_first=True
    )

    assert hits == [ALERT]
    body = json.loads(route.calls.last.request.content)
    assert body["size"] == 50
    assert body["sort"] == [
        {"@timestamp": {"order": "asc"}},
        {"_doc": {"order": "asc"}},
    ]
    assert _range(body, "rule.level") == {"gte": 12}
    assert _range(body, "@timestamp") == {
        "gte": start.isoformat() + "Z",
        "lte": end.isoformat() + "Z",
    }
    await ingestion._get_elastic_service().close()


@pytest.mark.asyncio
async def test_with_kibana_the_detections_api_is_still_used():
    ingestion = _ingestion(kibana_url="https://kibana.test:5601")
    svc = ingestion._get_elastic_service()
    svc.fetch_detection_alerts = AsyncMock(return_value={"hits": {"hits": []}})
    svc.search = AsyncMock()
    await ingestion.fetch_alerts()
    svc.fetch_detection_alerts.assert_awaited_once()
    svc.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_sync_is_not_supported_without_kibana():
    ingestion = _ingestion()
    svc = ingestion._get_elastic_service()
    svc.update_alert_status = AsyncMock()
    assert await ingestion.update_upstream_alert_status("abc123", "closed") is False
    svc.update_alert_status.assert_not_awaited()


@pytest.mark.parametrize(
    "level,expected",
    [(3, "info"), (4, "low"), (7, "medium"), (10, "high"), (12, "critical")],
)
def test_severity_for_rule_level(level, expected):
    assert severity_for_rule_level(level) == expected


def test_is_wazuh_alert_needs_a_rule_level_and_no_detection_fields():
    assert is_wazuh_alert(ALERT["_source"])
    assert not is_wazuh_alert({"rule": {"name": "x", "id": "y"}})  # ECS rule
    assert not is_wazuh_alert(
        {"kibana.alert.rule.name": "x", "rule": {"level": 10, "description": "y"}}
    )
    assert not is_wazuh_alert({"kibana.alert.uuid": "u", "rule": {"level": 10}})
    assert not is_wazuh_alert({"signal": {"rule": {}}, "rule": {"level": 10}})


def test_transform_maps_wazuh_fields():
    finding = _ingestion().transform_alert_to_finding(ALERT)

    assert finding["finding_id"] == "elastic-abc123"
    assert finding["data_source"] == "elastic"
    assert finding["severity"] == "high"
    assert finding["title"] == ALERT["_source"]["rule"]["description"]
    assert finding["description"].startswith("Sep 28 11:17:17 web01")
    assert finding["timestamp"] == "2026-09-28T11:17:18.394Z"
    assert finding["mitre_predictions"] == {"T1110": 0.9}
    assert finding["entity_context"] == {
        "src_ips": ["203.0.113.7"],
        "dest_ips": [],
        "hostnames": ["web01"],
        "usernames": ["root", "svc-backup"],
    }
    meta = finding["metadata"]
    assert meta["vendor"] == "wazuh"
    assert meta["rule_id"] == "5712"
    assert meta["rule_level"] == 10
    assert meta["agent_name"] == "web01"


def test_transform_manager_syslog_and_windows_fields():
    alert = {
        "_id": "m1",
        "_source": {
            "agent": {"id": "000", "name": "wazuh-manager"},
            "predecoder": {"hostname": "fw01"},
            "data": {
                "srcuser": "mallory",
                "win": {
                    "eventdata": {"subjectUserName": "admin", "ipAddress": "10.1.2.3"},
                    "system": {"message": "An account failed to log on."},
                },
            },
            "rule": {
                "level": 5,
                "description": "Logon failure",
                "mitre": {"id": "T1078"},
            },
        },
    }
    finding = _ingestion().transform_alert_to_finding(alert)
    assert finding["entity_context"]["hostnames"] == ["wazuh-manager", "fw01"]
    assert finding["entity_context"]["usernames"] == ["mallory", "admin"]
    assert finding["entity_context"]["src_ips"] == ["10.1.2.3"]
    assert finding["description"] == "An account failed to log on."
    assert finding["mitre_predictions"] == {"T1078": 0.9}


def test_elastic_security_alerts_keep_the_ecs_transform():
    alert = {
        "_id": "k1",
        "_source": {
            "kibana.alert.rule.name": "Suspicious PowerShell",
            "kibana.alert.severity": "high",
            "rule": {"name": "Suspicious PowerShell", "id": "r-1"},
            "host": {"name": "WS-01"},
        },
    }
    finding = _ingestion().transform_alert_to_finding(alert)
    assert finding["title"] == "Suspicious PowerShell"
    assert finding["severity"] == "high"
    assert finding["entity_context"]["hostnames"] == ["WS-01"]
    assert "vendor" not in finding["metadata"]
