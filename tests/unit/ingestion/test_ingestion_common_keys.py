"""Alert files in the common shape (id, title, source, iocs, raw_data,
mitre_techniques) import with their content kept."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.ingestion.ingestion_jobs import summarize_stats  # noqa: E402
from core.ingestion.ingestion_service import (  # noqa: E402
    IngestionService,
    to_internal_finding,
)

pytestmark = pytest.mark.unit

FIXTURE = REPO / "tests" / "fixtures" / "sample_findings.json"


class _CaptureDb:
    def __init__(self, first_error=None):
        self.batch: List[Dict[str, Any]] = []
        self._first_error = first_error

    def bulk_create_findings(self, rows):
        if self._first_error:
            return {
                "imported": 0,
                "skipped": 0,
                "errors": len(rows),
                "first_error": self._first_error,
            }
        self.batch.extend(rows)
        return {"imported": len(rows), "skipped": 0}


@pytest.fixture
def service():
    svc = IngestionService()
    svc.use_database = True
    svc.db_service = _CaptureDb()
    return svc


def test_sample_findings_import_with_every_field_kept(service):
    stats = service.ingest_json_file(FIXTURE)

    expected = json.loads(FIXTURE.read_text())
    assert stats["findings_imported"] == len(expected)
    assert stats["findings_errors"] == 0
    for src, row in zip(expected, service.db_service.batch):
        assert row["finding_id"] == src["id"]
        assert row["title"] == src["title"]
        assert row["data_source"] == src["source"]
        assert row["description"] == src["description"]
        assert row["external_id"] == src["external_id"]
        context = row["entity_context"]
        assert {k: context[k] for k in src["iocs"]} == src["iocs"]
        evidence = context["source_evidence"]
        assert evidence["telemetry_kind"] == "generic_log"
        assert evidence["status"] == "available"
        assert evidence["records"] == [src["raw_data"]]
        assert row["mitre_predictions"] == {t: 1.0 for t in src["mitre_techniques"]}


def test_id_keyed_top_level_array_is_recognised_without_streaming(service):
    service._ingest_json_full(FIXTURE)

    assert len(service.db_service.batch) == 5


def test_mixed_shapes_import_and_the_internal_key_wins(service):
    rows = [
        {"id": "common", "title": "Common", "source": "splunk"},
        {
            "finding_id": "internal",
            "id": "ignored",
            "data_source": "flow",
            "source": "ignored",
            "entity_context": {"host": "a", "shared": "internal"},
            "iocs": {"ips": ["1.2.3.4"], "shared": "ioc"},
            "mitre_predictions": {"T1059": 0.4},
            "mitre_techniques": ["T1027"],
        },
    ]

    service._ingest_finding_batch(rows)

    common, internal = service.db_service.batch
    assert (common["finding_id"], common["data_source"]) == ("common", "splunk")
    assert internal["finding_id"] == "internal"
    assert internal["data_source"] == "flow"
    assert internal["entity_context"] == {
        "host": "a",
        "ips": ["1.2.3.4"],
        "shared": "internal",
    }
    assert internal["mitre_predictions"] == {"T1059": 0.4}
    assert "id" not in internal and "source" not in internal


def test_mapping_does_not_mutate_the_callers_row():
    row = {"id": "f1", "iocs": {"ips": ["1.2.3.4"]}, "raw_data": "raw line"}

    mapped = to_internal_finding(row)

    assert row == {"id": "f1", "iocs": {"ips": ["1.2.3.4"]}, "raw_data": "raw line"}
    assert mapped["entity_context"]["source_evidence"]["raw_text"] == "raw line"


def test_a_row_without_any_id_names_the_reason(service):
    service._ingest_finding_batch([{"title": "no id"}])

    assert service.stats["findings_errors"] == 1
    assert service.first_error == "Finding missing id/finding_id"
    _, message = summarize_stats(service.stats, service.first_error)
    assert "1 finding errors. First error: Finding missing id/finding_id" in message


def test_only_the_first_error_is_kept(service):
    service._ingest_finding_batch([{"title": "a"}, {"title": "b"}])

    assert service.stats["findings_errors"] == 2
    assert service.first_error == "Finding missing id/finding_id"


def test_a_database_error_reaches_the_message():
    svc = IngestionService()
    svc.use_database = True
    svc.db_service = _CaptureDb(first_error="Finding f1: value too long")

    svc._ingest_finding_batch([{"id": "f1"}])

    assert svc.first_error == "Finding f1: value too long"
