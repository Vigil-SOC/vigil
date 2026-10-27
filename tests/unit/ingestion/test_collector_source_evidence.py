"""Splunk, Elastic, OpenSearch and CrowdStrike store the raw alert.

Ingest already maps ``raw_data`` onto ``entity_context.source_evidence`` and
leaves a collector-supplied envelope in place. These builders used to emit
``raw_event``, which nothing read.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

import pytest

from core.findings.source_evidence import SOURCE_EVIDENCE_RAW_TEXT_LIMIT
from core.ingestion.ingestion_service import IngestionService
from core.integrations.crowdstrike.adapter import _detection_to_finding
from core.integrations.elastic.ingestion import ElasticIngestion
from core.integrations.opensearch.ingestion import OpenSearchIngestion
from core.integrations.splunk.adapter import _splunk_event_to_finding
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit

Alert = Dict[str, Any]
Finding = Dict[str, Any]
Builder = Callable[[], Tuple[Alert, Finding]]

_SUPPLIED: Dict[str, Any] = {
    "version": 1,
    "telemetry_kind": "dns",
    "schema_id": "resolver-events.v3",
    "status": "available",
    "provenance": "joined",
    "total_records": 1,
    "truncated": False,
    "records": [{"query": "example.test"}],
}


class _CaptureDb:
    def __init__(self) -> None:
        self.created: List[Dict[str, Any]] = []

    def get_finding(self, finding_id: str) -> None:
        return None

    def get_finding_by_external_id(self, data_source: str, external_id: str) -> None:
        return None

    def create_finding(self, **kwargs: Any) -> Dict[str, Any]:
        self.created.append(kwargs)
        return kwargs


def _stored(finding: Finding) -> Dict[str, Any]:
    svc = IngestionService()
    capture = _CaptureDb()
    svc.use_database = True
    svc.db_service = capture
    assert svc.ingest_finding(finding) is True
    evidence = capture.created[-1]["entity_context"]["source_evidence"]
    assert isinstance(evidence, dict)
    return evidence


def _require(finding: Finding | None) -> Finding:
    assert finding is not None
    return finding


def _splunk_adapter() -> Tuple[Alert, Finding]:
    event = {
        "_cd": "cd-1",
        "search_name": "Notable",
        "urgency": "high",
        "_raw": "src=10.0.0.1",
        "src_ip": "10.0.0.1",
    }
    return event, _require(_splunk_event_to_finding(event))


def _splunk_legacy() -> Tuple[Alert, Finding]:
    event = {
        "_cd": "cd-legacy",
        "search_name": "Legacy notable",
        "urgency": "medium",
        "_raw": "user=alice",
        "user": "alice",
    }
    poller = DataPoller.__new__(DataPoller)
    return event, _require(poller._splunk_event_to_finding(event))


def _crowdstrike_adapter() -> Tuple[Alert, Finding]:
    detection = {
        "detection_id": "ldt:adapter",
        "max_severity_displayname": "High",
        "scenario": "Credential dump",
        "description": "detected",
        "behaviors": [{"technique": "T1003"}],
        "device": {"local_ip": "10.1.1.1", "hostname": "host-a", "device_id": "dev-1"},
        "user_name": "alice",
        "max_confidence": 80,
    }
    return detection, _require(_detection_to_finding(detection))


def _crowdstrike_legacy() -> Tuple[Alert, Finding]:
    detection = {
        "detection_id": "ldt:legacy",
        "max_severity_displayname": "Critical",
        "scenario": "Ransomware",
        "description": "blocked",
        "behaviors": [{"technique": "T1486"}],
        "device": {"local_ip": "10.2.2.2", "hostname": "host-b", "device_id": "dev-2"},
        "user_name": "bob",
        "max_confidence": 90,
    }
    poller = DataPoller.__new__(DataPoller)
    return detection, _require(poller._crowdstrike_detection_to_finding(detection))


def _elastic(source: Dict[str, Any], alert_id: str) -> Tuple[Alert, Finding]:
    alert = {"_id": alert_id, "_index": "signals", "_source": source}
    ingestion = ElasticIngestion.__new__(ElasticIngestion)
    return alert, _require(ingestion.transform_alert_to_finding(alert))


def _elastic_kibana() -> Tuple[Alert, Finding]:
    return _elastic(
        {
            "kibana.alert.rule.name": "Suspicious PowerShell",
            "kibana.alert.severity": "high",
            "@timestamp": "2026-01-01T00:00:00Z",
            "host": {"name": "WORKSTATION-01"},
        },
        "kibana-1",
    )


def _elastic_wazuh() -> Tuple[Alert, Finding]:
    return _elastic(
        {
            "rule": {"level": 10, "description": "Wazuh rule", "id": "5710"},
            "@timestamp": "2026-01-01T00:00:00Z",
            "full_log": "syslog line",
            "agent": {"name": "host-1", "id": "001"},
        },
        "wazuh-1",
    )


def _opensearch() -> Tuple[Alert, Finding]:
    alert = {
        "_id": "os-1",
        "_index": ".opensearch-sap-windows-findings-1",
        "_source": {
            "id": "os-1",
            "monitor_name": "Detector",
            "queries": [
                {"id": "r1", "name": "Suspicious logon", "tags": ["attack.t1078"]}
            ],
            "timestamp": 1760000000000,
        },
    }
    ingestion = OpenSearchIngestion.__new__(OpenSearchIngestion)
    return alert, _require(ingestion.transform_alert_to_finding(alert))


BUILDERS: List[Tuple[str, Builder]] = [
    ("splunk-adapter", _splunk_adapter),
    ("splunk-legacy", _splunk_legacy),
    ("crowdstrike-adapter", _crowdstrike_adapter),
    ("crowdstrike-legacy", _crowdstrike_legacy),
    ("elastic-kibana", _elastic_kibana),
    ("elastic-wazuh", _elastic_wazuh),
    ("opensearch", _opensearch),
]


@pytest.mark.parametrize(
    "build", [build for _, build in BUILDERS], ids=[name for name, _ in BUILDERS]
)
def test_ingest_stores_the_raw_alert(build: Builder) -> None:
    alert, finding = build()
    assert "raw_event" not in finding
    assert finding["raw_data"] == alert

    evidence = _stored(finding)

    assert evidence["telemetry_kind"] == "generic_log"
    assert evidence["schema_id"] == "generic-log.v1"
    assert evidence["status"] == "available"
    assert evidence["provenance"] == "embedded"
    assert evidence["truncated"] is False
    assert evidence["records"] == [alert]


@pytest.mark.parametrize(
    "build", [build for _, build in BUILDERS], ids=[name for name, _ in BUILDERS]
)
def test_collector_supplied_source_evidence_is_unchanged(build: Builder) -> None:
    _alert, finding = build()
    finding["entity_context"]["source_evidence"] = _SUPPLIED

    assert _stored(finding) == _SUPPLIED


def _long_splunk_event() -> Alert:
    return {
        "_cd": "cd-long",
        "search_name": "Long raw",
        "urgency": "low",
        "_raw": "R" * (SOURCE_EVIDENCE_RAW_TEXT_LIMIT + 25),
        "host": "splunk-host",
    }


def _long_splunk_adapter() -> Finding:
    return _require(_splunk_event_to_finding(_long_splunk_event()))


def _long_splunk_legacy() -> Finding:
    return _require(
        DataPoller.__new__(DataPoller)._splunk_event_to_finding(_long_splunk_event())
    )


@pytest.mark.parametrize(
    "build",
    [_long_splunk_adapter, _long_splunk_legacy],
    ids=["splunk-adapter", "splunk-legacy"],
)
def test_oversized_splunk_raw_is_stored_cut_to_the_limit(
    build: Callable[[], Finding],
) -> None:
    finding = build()
    raw = finding["raw_data"]["_raw"]
    evidence = _stored(finding)
    stored = evidence["records"][0]

    assert stored["_raw"] == raw[:SOURCE_EVIDENCE_RAW_TEXT_LIMIT]
    assert len(stored["_raw"]) == SOURCE_EVIDENCE_RAW_TEXT_LIMIT
    assert stored["host"] == "splunk-host"
    assert stored["search_name"] == "Long raw"
    assert evidence["truncated"] is True
