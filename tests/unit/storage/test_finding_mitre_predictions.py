"""Child-table MITRE predictions: dump reconstruction and ATT&CK readers."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from core.storage.models import Case, Finding, FindingMitrePrediction
from core.storage.schemas.case import CaseWithFindingsSchema
from core.storage.schemas.finding import FindingSchema
from core.storage.service import (
    _numeric_prediction_items,
    findings_by_technique_stmt,
    technique_severity_counts_stmt,
)
from core.threat_intel import attack_router

pytestmark = pytest.mark.unit


def test_dump_reconstructs_map_including_tactic_names():
    finding = Finding(
        finding_id="f-1",
        anomaly_score=0.1,
        timestamp=datetime(2024, 1, 1),
        data_source="test",
    )
    finding.mitre_prediction_rows = [
        FindingMitrePrediction(technique_id="T1071.001", confidence=0.875),
        FindingMitrePrediction(technique_id="Initial Access", confidence=0.5),
    ]
    assert FindingSchema.dump(finding)["mitre_predictions"] == {
        "T1071.001": 0.875,
        "Initial Access": 0.5,
    }


def test_dump_empty_rows_emits_empty_map():
    finding = Finding()
    assert FindingSchema.dump(finding)["mitre_predictions"] == {}


def test_nested_case_dump_reconstructs_predictions_from_rows():
    finding = Finding(
        finding_id="f-1",
        anomaly_score=0.1,
        timestamp=datetime(2024, 1, 1),
        data_source="test",
    )
    finding.mitre_prediction_rows = [
        FindingMitrePrediction(technique_id="T1071.001", confidence=0.875),
    ]
    case = Case(case_id="c-1", title="nested dump")
    case.findings = [finding]
    dumped = CaseWithFindingsSchema.dump(case)
    assert dumped["findings"][0]["mitre_predictions"] == {"T1071.001": 0.875}


def test_numeric_items_keep_tactic_names_and_skip_non_numeric():
    items = dict(
        _numeric_prediction_items(
            {
                "T1071.001": 0.875,
                "Initial Access": 1,
                "nested": {"nope": 1},
                "label": "ransomware",
                "flag": True,
            }
        )
    )
    assert items == {"T1071.001": 0.875, "Initial Access": 1.0}


def test_get_findings_by_technique_queries_child_table(monkeypatch):
    service = MagicMock()
    service.is_using_database.return_value = True
    service.get_findings_by_technique.return_value = [{"finding_id": "f-1"}]
    monkeypatch.setattr(attack_router, "data_service", service)

    result = attack_router.get_findings_by_technique("T1071.001")

    service.get_findings_by_technique.assert_called_once_with(
        "T1071.001",
        exclusions="hide",
        min_confidence=0.0,
        start_time=None,
        end_time=None,
    )
    service.get_findings.assert_not_called()
    assert result["total"] == 1
    assert result["findings"][0]["finding_id"] == "f-1"


def test_attck_readers_fall_back_to_finding_maps_without_a_database(monkeypatch):
    service = MagicMock()
    service.is_using_database.return_value = False
    service.get_findings.return_value = [
        {
            "finding_id": "f-1",
            "severity": "high",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "mitre_predictions": {"T1071.001": 0.9},
        },
        {
            "finding_id": "f-2",
            "severity": "low",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "mitre_predictions": {"T1059.001": 0.1},
        },
    ]
    monkeypatch.setattr(attack_router, "data_service", service)

    by_tech = attack_router.get_findings_by_technique("T1071.001")
    rollup = attack_router.occurrence_rollup(time_range="all")

    service.get_findings_by_technique.assert_not_called()
    assert by_tech["total"] == 1
    assert rollup["techniques"][0]["count"] == 1


def test_rollup_queries_child_table(monkeypatch):
    service = MagicMock()
    service.is_using_database.return_value = True
    service.get_technique_severity_counts.return_value = [
        ("T1071.001", "high", 2),
    ]
    monkeypatch.setattr(attack_router, "data_service", service)

    rollup = attack_router.occurrence_rollup()

    service.get_findings.assert_not_called()
    # The dashboard counts the queue, which analyst-excluded IPs are not in.
    assert (
        service.get_technique_severity_counts.call_args.kwargs["exclusions"] == "hide"
    )
    assert rollup["total_techniques"] == 1
    assert rollup["techniques"][0]["count"] == 2


@pytest.mark.asyncio
async def test_rollup_run_id_returns_coverage_not_occurrence(monkeypatch):
    async def fake_analyze(**kwargs):
        assert kwargs == {"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        return {
            "run_id": kwargs["run_id"],
            "techniques": [
                {
                    "technique_id": "T1059.001",
                    "verdict": "rule",
                    "steps": [],
                    "missed": [{"id": "step-2", "index": 1, "citations": []}],
                }
            ],
        }

    class _Tools:
        analyze_coverage = staticmethod(fake_analyze)

    monkeypatch.setattr(attack_router, "get_security_detection_tools", lambda: _Tools())
    result = await attack_router.get_technique_rollup(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert result["total_techniques"] == 1
    assert result["techniques"][0]["verdict"] == "rule"
    assert "count" not in result["techniques"][0]
    assert result["techniques"][0]["missed"][0]["id"] == "step-2"


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# The column is NOT NULL until #3221, so the threshold is pinned on the SQL.
@pytest.mark.parametrize(
    "stmt", [technique_severity_counts_stmt, findings_by_technique_stmt]
)
def test_a_null_confidence_passes_at_zero_and_fails_above(stmt):
    args = ("T1071",) if stmt is findings_by_technique_stmt else ()
    at_zero = _sql(stmt(*args, min_confidence=0.0))
    assert "finding_mitre_predictions.confidence >= 0.0 OR" in at_zero
    assert "finding_mitre_predictions.confidence IS NULL" in at_zero
    above = _sql(stmt(*args, min_confidence=0.5))
    assert "finding_mitre_predictions.confidence >= 0.5" in above
    assert "IS NULL" not in above


def test_drill_down_does_not_let_the_database_order_unknowns_first():
    assert "ORDER BY" not in _sql(findings_by_technique_stmt("T1071"))


def _in_memory(monkeypatch, findings):
    service = MagicMock()
    service.is_using_database.return_value = False
    service.get_findings.return_value = findings
    monkeypatch.setattr(attack_router, "data_service", service)


MIXED = [
    {
        "finding_id": "f-unknown",
        "severity": "high",
        "mitre_predictions": {"T1071": None, "T1059": 0.9},
    },
    {"finding_id": "f-low", "severity": "low", "mitre_predictions": {"T1071": 0.2}},
    {"finding_id": "f-high", "severity": "low", "mitre_predictions": {"T1071": 0.8}},
]


def test_rollup_counts_unknowns_at_zero_and_excludes_them_above(monkeypatch):
    _in_memory(monkeypatch, MIXED)

    at_zero = attack_router.occurrence_rollup()
    counts = {t["technique_id"]: t["count"] for t in at_zero["techniques"]}
    assert counts == {"T1071": 3, "T1059": 1}
    assert at_zero["excluded_occurrences"] == 0

    above = attack_router.occurrence_rollup(min_confidence=0.5)
    counts = {t["technique_id"]: t["count"] for t in above["techniques"]}
    assert counts == {"T1071": 1, "T1059": 1}
    # Only the unknown counts as excluded, not the 0.2 below the threshold.
    assert above["excluded_occurrences"] == 1


def test_database_rollup_reports_unknowns_only_above_zero(monkeypatch):
    service = MagicMock()
    service.is_using_database.return_value = True
    service.get_technique_severity_counts.return_value = []
    service.count_unscored_technique_occurrences.return_value = 7
    monkeypatch.setattr(attack_router, "data_service", service)

    assert attack_router.occurrence_rollup()["excluded_occurrences"] == 0
    service.count_unscored_technique_occurrences.assert_not_called()
    assert attack_router.occurrence_rollup(0.5)["excluded_occurrences"] == 7


def test_drill_down_lists_scored_then_unknown_and_follows_the_slider(monkeypatch):
    _in_memory(monkeypatch, MIXED)

    def ids(**kwargs):
        body = attack_router.get_findings_by_technique("T1071", **kwargs)
        return [f["finding_id"] for f in body["findings"]]

    assert ids() == ["f-high", "f-low", "f-unknown"]
    assert ids(min_confidence=0.5) == ["f-high"]
