"""Tests for the generic finding source-evidence contract."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

from services.ingestion_service import IngestionService
from services.source_evidence import (
    SOURCE_EVIDENCE_PREVIEW_LIMIT,
    normalize_source_evidence,
    normalize_finding_source_evidence,
    project_finding_source_evidence_for_list,
    source_evidence_from_loglm_row,
)

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
_FINDINGS_SPEC = importlib.util.spec_from_file_location(
    "source_evidence_findings_api", REPO / "backend" / "api" / "findings.py"
)
assert _FINDINGS_SPEC and _FINDINGS_SPEC.loader
findings_api = importlib.util.module_from_spec(_FINDINGS_SPEC)
_FINDINGS_SPEC.loader.exec_module(findings_api)


def test_legacy_loglm_events_and_sequence_become_generic_evidence():
    evidence = source_evidence_from_loglm_row(
        {
            "events_json": json.dumps([
                {"timestamp": "2026-07-21T12:00:00Z", "event_type": "request"},
                {"timestamp": "2026-07-21T12:00:01Z", "event_type": "response"},
            ]),
            "sequence": "request -> response",
        }
    )

    assert evidence == {
        "version": 1,
        "telemetry_kind": "generic_log",
        "schema_id": "generic-log.v1",
        "status": "available",
        "provenance": "embedded",
        "total_records": 2,
        "truncated": False,
        "records": [
            {"timestamp": "2026-07-21T12:00:00Z", "event_type": "request"},
            {"timestamp": "2026-07-21T12:00:01Z", "event_type": "response"},
        ],
        "raw_text": "request -> response",
        "raw_text_truncated": False,
    }


def test_explicit_kind_selects_dns_renderer_without_data_source_guessing():
    evidence = source_evidence_from_loglm_row(
        {
            "source_evidence_kind": "dns",
            "source_evidence_schema_id": "resolver-events.v3",
            "events_json": [{"query": "example.test", "query_type": "A"}],
        }
    )

    assert evidence["telemetry_kind"] == "dns"
    assert evidence["schema_id"] == "resolver-events.v3"
    assert evidence["records"] == [{"query": "example.test", "query_type": "A"}]


def test_declared_missing_evidence_is_truthful_and_payload_free():
    evidence = source_evidence_from_loglm_row(
        {"source_evidence_kind": "netflow", "source_evidence_status": "not_in_artifact"}
    )

    assert evidence["status"] == "not_in_artifact"
    assert "records" not in evidence
    assert "raw_text" not in evidence


def test_malformed_explicit_evidence_fails_closed_as_invalid():
    evidence = normalize_source_evidence(
        {
            "version": 1,
            "telemetry_kind": "netflow",
            "schema_id": "netflow.v1",
            "status": "available",
            "provenance": "embedded",
            "records": "not-a-list",
        }
    )

    assert evidence["status"] == "invalid"
    assert "records" not in evidence


def test_unknown_explicit_kind_fails_closed_instead_of_guessing():
    evidence = source_evidence_from_loglm_row(
        {
            "source_evidence_kind": "packet_magic",
            "events_json": [{"message": "event"}],
        }
    )

    assert evidence["status"] == "invalid"
    assert evidence["telemetry_kind"] == "generic_log"


def test_preview_is_bounded_and_non_finite_values_are_json_safe():
    records = [{"index": index, "value": float("nan")} for index in range(140)]
    evidence = normalize_source_evidence(
        {
            "version": 1,
            "telemetry_kind": "generic_log",
            "schema_id": "generic-log.v1",
            "status": "available",
            "provenance": "embedded",
            "total_records": len(records),
            "records": records,
        }
    )

    assert len(evidence["records"]) == SOURCE_EVIDENCE_PREVIEW_LIMIT
    assert evidence["total_records"] == 140
    assert evidence["truncated"] is True
    assert evidence["records"][0]["value"] is None


def test_parquet_mapper_persists_source_evidence_in_entity_context():
    service = IngestionService.__new__(IngestionService)
    finding = service._parquet_row_to_finding(
        {
            "sequence_id": "sequence-1",
            "event_start_time": 1_784_653_712_000,
            "incident_pred": 1,
            "confidence_score": 0.91,
            "events_json": [{"event_type": "connection"}],
            "sequence": "connection event",
            "source_evidence_kind": "generic_log",
        }
    )

    evidence = finding["entity_context"]["source_evidence"]
    assert evidence["status"] == "available"
    assert evidence["records"] == [{"event_type": "connection"}]
    assert evidence["raw_text"] == "connection event"


def _finding_with_evidence():
    return {
        "finding_id": "f-source-1",
        "entity_context": {
            "hostname": "host-1",
            "source_evidence": {
                "version": 1,
                "telemetry_kind": "generic_log",
                "schema_id": "generic-log.v1",
                "status": "available",
                "provenance": "embedded",
                "total_records": 1,
                "truncated": False,
                "records": [{"message": "raw event"}],
                "raw_text": "raw event",
            },
        },
    }


def test_list_projection_strips_payload_without_mutating_stored_finding():
    finding = _finding_with_evidence()
    original = deepcopy(finding)

    projected = project_finding_source_evidence_for_list(finding)
    evidence = projected["entity_context"]["source_evidence"]

    assert "records" not in evidence
    assert "raw_text" not in evidence
    assert evidence["payload_included"] is False
    assert finding == original


def test_detail_normalization_bounds_existing_unbounded_evidence():
    finding = _finding_with_evidence()
    finding["entity_context"]["source_evidence"]["total_records"] = 130
    finding["entity_context"]["source_evidence"]["records"] = [
        {"message": f"event-{index}"} for index in range(130)
    ]

    normalized = normalize_finding_source_evidence(finding)
    evidence = normalized["entity_context"]["source_evidence"]

    assert len(evidence["records"]) == SOURCE_EVIDENCE_PREVIEW_LIMIT
    assert evidence["truncated"] is True
    assert len(finding["entity_context"]["source_evidence"]["records"]) == 130


class _FakeDataService:
    def __init__(self, finding):
        self.finding = finding

    def is_s3_configured(self):
        return False

    def count_findings(self, **_kwargs):
        return 1

    def get_findings(self, **_kwargs):
        return [self.finding]

    def get_finding(self, finding_id):
        return self.finding if finding_id == self.finding["finding_id"] else None


def test_findings_list_omits_payload_while_detail_retains_it(monkeypatch):
    finding = _finding_with_evidence()
    monkeypatch.setattr(findings_api, "data_service", _FakeDataService(finding))

    list_response = asyncio.run(findings_api.get_findings(
        severity=None,
        data_source=None,
        cluster_id=None,
        min_anomaly_score=None,
        status=None,
        search=None,
        offset=0,
        limit=100,
        sort_by="timestamp",
        sort_order="desc",
        force_refresh=False,
    ))
    detail_response = asyncio.run(findings_api.get_finding("f-source-1"))

    listed = list_response["findings"][0]["entity_context"]["source_evidence"]
    detailed = detail_response["entity_context"]["source_evidence"]
    assert "records" not in listed
    assert "raw_text" not in listed
    assert detailed["records"] == [{"message": "raw event"}]
    assert detailed["raw_text"] == "raw event"
