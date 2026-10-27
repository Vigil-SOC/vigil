"""An unscored finding must not crash the operator enrichment script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "enrich_findings_under_test",
        _REPO / "scripts" / "enrich_findings.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_and_export_print_not_scored_and_a_real_zero(tmp_path, capsys, monkeypatch):
    module = _load()
    findings = [
        {
            "finding_id": "f-none",
            "data_source": "splunk",
            "anomaly_score": None,
            "severity": "medium",
            "mitre_predictions": {},
        },
        {
            "finding_id": "f-zero",
            "data_source": "crowdstrike",
            "anomaly_score": 0.0,
            "severity": "info",
            "mitre_predictions": {},
        },
    ]
    monkeypatch.setattr(module.data_service, "get_findings", lambda: findings)

    module.list_unenriched_findings()
    listed = capsys.readouterr().out
    assert "not scored" in listed
    assert "0.00" in listed

    dest = tmp_path / "template.json"
    module.export_enrichment_template(dest)
    template = json.loads(dest.read_text())
    assert "not scored" in template["f-none"]["_comment"]
    assert "0.00" in template["f-zero"]["_comment"]
