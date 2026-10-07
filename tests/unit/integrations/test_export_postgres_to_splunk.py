"""HEC ``time`` for export_postgres_to_splunk.py must be the true UTC epoch.

The model columns are naive ``DateTime`` holding UTC. A naive
``datetime.timestamp()`` is read as host-local time, so on a non-UTC host the
exported events landed hours off in Splunk.
"""

import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))

import export_postgres_to_splunk as exporter_mod  # noqa: E402

NAIVE_UTC = datetime(2026, 1, 15, 12, 0, 0)
TRUE_EPOCH = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def chicago_tz(monkeypatch):
    """Run under a non-UTC local zone, restoring the process zone afterwards."""
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def exporter(monkeypatch):
    # Never touch the network, and keep the schema dump out of the way.
    def _no_network(*args, **kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(exporter_mod.requests, "post", _no_network)
    monkeypatch.setattr(
        exporter_mod.FindingSchema, "dump", lambda finding: {"mitre_predictions": {}}
    )
    return exporter_mod.PostgresToSplunkExporter(
        hec_url="http://localhost", hec_token="dummy"
    )


def _finding(ts):
    return SimpleNamespace(
        finding_id="f-1",
        data_source="test",
        severity="high",
        status="new",
        anomaly_score=0.9,
        entity_context=None,
        cluster_id=None,
        created_at=NAIVE_UTC,
        updated_at=NAIVE_UTC,
        ai_enrichment=None,
        evidence_links=None,
        timestamp=ts,
    )


def _case(created_at):
    return SimpleNamespace(
        case_id="c-1",
        title="t",
        description="d",
        status="open",
        priority="high",
        assignee=None,
        tags=None,
        mitre_techniques=None,
        timeline=None,
        activities=None,
        notes=None,
        resolution_steps=None,
        findings=[],
        created_at=created_at,
        updated_at=created_at,
    )


def test_finding_time_is_true_utc_epoch(chicago_tz, exporter):
    event = exporter.finding_to_splunk_event(_finding(NAIVE_UTC))
    assert event["time"] == TRUE_EPOCH


def test_case_time_is_true_utc_epoch(chicago_tz, exporter):
    event = exporter.case_to_splunk_event(_case(NAIVE_UTC))
    assert event["time"] == TRUE_EPOCH


def test_aware_value_is_left_as_is(chicago_tz, exporter):
    aware = datetime(2026, 1, 15, 6, 0, 0, tzinfo=timezone.utc)
    event = exporter.finding_to_splunk_event(_finding(aware))
    assert event["time"] == aware.timestamp()


def test_missing_time_falls_back_to_now(chicago_tz, exporter):
    before = time.time()
    finding_event = exporter.finding_to_splunk_event(_finding(None))
    case_event = exporter.case_to_splunk_event(_case(None))
    after = time.time()
    assert before <= finding_event["time"] <= after
    assert before <= case_event["time"] <= after
