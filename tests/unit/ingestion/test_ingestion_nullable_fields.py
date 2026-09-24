from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.ingestion.ingestion_service import (  # noqa: E402
    ID_HASH_WIDTH,
    IngestionService,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    return IngestionService()


def _parquet_classified(**overrides):
    """LogLM row with a classification and embedding but no score/time/severity."""
    row = {
        "sequence_id": "seq-null-fields",
        "mitre_pred": 7,
        "embedding": [0.1, 0.2, 0.3],
        "focal_ip": "10.0.0.1",
        "engaged_ip": "10.0.0.2",
    }
    row.update(overrides)
    return row


class _CaptureDb:
    def __init__(self):
        self.created: Dict[str, Any] = {}
        self.batch: List[Dict[str, Any]] = []

    def get_finding(self, finding_id):
        return None

    def create_finding(self, **kwargs):
        self.created.update(kwargs)
        return object()

    def bulk_create_findings(self, rows):
        self.batch.extend(rows)
        return {"imported": len(rows), "skipped": 0}


def test_parquet_null_score_severity_and_time_stay_null(service):
    finding = service._parquet_row_to_finding(_parquet_classified())

    assert finding["anomaly_score"] is None
    assert finding["severity"] is None
    assert finding["timestamp"] is None
    assert finding["mitre_predictions"]["Command and Control"] == 1.0


def test_parquet_does_not_derive_score_or_severity_from_incident_pred(service):
    finding = service._parquet_row_to_finding(
        _parquet_classified(incident_pred=1, confidence_score=None)
    )

    assert finding["anomaly_score"] is None
    assert finding["severity"] is None
    assert finding["entity_context"]["incident_pred"] == 1
    assert "confidence_score" not in finding["entity_context"]


def test_tempo_csv_null_confidence_and_time_stay_null(service):
    finding = service._tempo_csv_row_to_finding(
        {
            "sequence_id": "s-1",
            "mitre_tactic": "Command and Control",
            "IP1": "10.0.0.1",
            "IP2": "10.0.0.2",
        }
    )

    assert finding["anomaly_score"] is None
    assert finding["severity"] is None
    assert finding["timestamp"] is None
    assert finding["mitre_predictions"] == {"Command and Control": 1.0}


def test_generic_row_does_not_invent_score_or_time(service):
    finding = service._generic_row_to_finding({"src_ip": "10.0.0.1"})

    assert finding["anomaly_score"] is None
    assert finding["timestamp"] is None
    assert finding["severity"] is None


def test_parse_timestamp_leaves_missing_and_unparseable_as_none(service):
    assert service.parse_timestamp(None) is None
    assert service.parse_timestamp("") is None
    assert service.parse_timestamp("not-a-time") is None
    assert service.parse_timestamp(float("nan")) is None


def test_parse_timestamp_converts_offsets_to_naive_utc(service):
    assert service.parse_timestamp("2026-09-22T18:06:15.000-03:00") == datetime(
        2026, 9, 22, 21, 6, 15
    )
    # A late -03:00 time lands on the next UTC date (feeds the finding id date)
    assert service.parse_timestamp("2026-09-22T22:30:00-03:00") == datetime(
        2026, 9, 23, 1, 30
    )
    # UTC and offset-less strings keep today's naive values
    expected = datetime(2026, 9, 22, 18, 6, 15)
    for value in (
        "2026-09-22T18:06:15Z",
        "2026-09-22T18:06:15+00:00",
        "2026-09-22T18:06:15",
        "2026-09-22 18:06:15",
    ):
        assert service.parse_timestamp(value) == expected


def test_parse_timestamp_epoch_ignores_local_timezone(service, monkeypatch):
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    time.tzset()
    try:
        assert service.parse_timestamp(0) == datetime(1970, 1, 1)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_missing_event_time_id_is_stable_across_days(service, monkeypatch):
    row = _parquet_classified()

    monkeypatch.setattr(
        "core.ingestion.ingestion_service.utcnow",
        lambda: datetime(2026, 1, 1),
    )
    first = service._parquet_row_to_finding(row)["finding_id"]

    monkeypatch.setattr(
        "core.ingestion.ingestion_service.utcnow",
        lambda: datetime(2026, 9, 3),
    )
    second = service._parquet_row_to_finding(row)["finding_id"]

    assert first == second
    assert first.startswith("f-")
    assert "20260101" not in first
    assert "20260903" not in first
    assert len(first.rsplit("-", 1)[1]) == ID_HASH_WIDTH


def test_distinct_null_time_rows_do_not_collapse(service):
    a = service._parquet_row_to_finding(_parquet_classified(sequence_id="seq-a"))
    b = service._parquet_row_to_finding(_parquet_classified(sequence_id="seq-b"))

    assert a["finding_id"] != b["finding_id"]


def test_ingest_finding_persists_null_score_severity_and_time(service):
    db = _CaptureDb()
    service.use_database = True
    service.db_service = db

    ok = service.ingest_finding(
        {
            "finding_id": "f-null-roundtrip",
            "mitre_predictions": {"Command and Control": 0.9},
            "anomaly_score": None,
            "timestamp": None,
            "severity": None,
            "data_source": "flow",
        }
    )

    assert ok is True
    assert db.created["anomaly_score"] is None
    assert db.created["timestamp"] is None
    assert db.created["severity"] is None


def test_batch_ingest_does_not_fill_null_score_or_time(service):
    db = _CaptureDb()
    service.use_database = True
    service.db_service = db

    service._ingest_finding_batch(
        [
            {
                "finding_id": "f-null-batch",
                "anomaly_score": None,
                "timestamp": None,
                "severity": None,
            }
        ]
    )

    assert db.batch[0]["anomaly_score"] is None
    assert db.batch[0]["timestamp"] is None
    assert db.batch[0]["severity"] is None
