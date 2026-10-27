"""The workflow target block must not invent a score the source never sent."""

from core.workflows.workflows_service import WorkflowsService


def _finding(**overrides):
    finding = {
        "finding_id": "f-1",
        "severity": "medium",
        "data_source": "upload",
        "timestamp": "2026-10-10T13:02:11Z",
        "anomaly_score": None,
        "description": "Uploaded without a score column",
        "mitre_predictions": {},
    }
    finding.update(overrides)
    return finding


def _brief(monkeypatch, finding):
    class _Data:
        def get_finding(self, finding_id):
            assert finding_id == "f-1"
            return finding

    monkeypatch.setattr(
        "core.storage.database_data_service.DatabaseDataService",
        lambda: _Data(),
    )
    return WorkflowsService()._build_target_context({"finding_id": "f-1"})


def _score_line(brief: str) -> str:
    return next(
        line for line in brief.splitlines() if line.startswith("- Anomaly Score:")
    )


def test_unscored_target_says_the_source_supplied_no_score(monkeypatch):
    absent = _finding()
    del absent["anomaly_score"]
    for finding in (_finding(anomaly_score=None), absent):
        line = _score_line(_brief(monkeypatch, finding))
        assert line == "- Anomaly Score: not scored by the source"
        assert "None" not in line
        assert "0" not in line


def test_a_real_zero_score_stays_zero(monkeypatch):
    line = _score_line(_brief(monkeypatch, _finding(anomaly_score=0.0)))
    assert line == "- Anomaly Score: 0.00"


def _techniques_line(brief: str) -> str:
    return next(
        line
        for line in brief.splitlines()
        if line.startswith("- MITRE ATT&CK Techniques:")
    )


def test_target_names_the_findings_techniques_scored_and_unknown(monkeypatch):
    finding = _finding(mitre_predictions={"T1071": None, "T1059": 0.3})
    line = _techniques_line(_brief(monkeypatch, finding))
    assert line == "- MITRE ATT&CK Techniques: T1059, T1071"
    assert _techniques_line(_brief(monkeypatch, _finding())).endswith(": None")
