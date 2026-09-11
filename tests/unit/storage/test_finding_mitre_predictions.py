"""Child-table MITRE predictions: dump reconstruction and ATT&CK readers."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.storage.models import Case, Finding, FindingMitrePrediction
from core.storage.schemas.case import CaseWithFindingsSchema
from core.storage.schemas.finding import FindingSchema
from core.storage.service import _numeric_prediction_items
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

    service.get_findings_by_technique.assert_called_once_with("T1071.001")
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
    layer = attack_router.get_attack_layer()
    rollup = attack_router.occurrence_rollup(time_range="all")
    tactics = attack_router.get_tactics_summary()

    service.get_findings_by_technique.assert_not_called()
    assert by_tech["total"] == 1
    assert layer["techniques"][0]["techniqueID"] == "T1071.001"
    assert rollup["techniques"][0]["count"] == 1
    assert tactics["tactics"][0]["count"] == 1


def test_rollup_layer_and_tactics_query_child_table(monkeypatch):
    service = MagicMock()
    service.is_using_database.return_value = True
    service.get_technique_max_confidence.return_value = {"T1071.001": 0.9}
    service.get_technique_severity_counts.return_value = [
        ("T1071.001", "high", 2),
    ]
    service.get_technique_occurrence_counts.return_value = {"T1071.001": 2}
    monkeypatch.setattr(attack_router, "data_service", service)

    layer = attack_router.get_attack_layer()
    rollup = attack_router.occurrence_rollup()
    tactics = attack_router.get_tactics_summary()

    service.get_findings.assert_not_called()
    assert layer["techniques"][0]["techniqueID"] == "T1071.001"
    assert rollup["total_techniques"] == 1
    assert rollup["techniques"][0]["count"] == 2
    assert tactics["tactics"][0]["count"] == 2


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
