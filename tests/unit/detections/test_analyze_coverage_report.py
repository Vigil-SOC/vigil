"""Run-scoped coverage report from reconstruct_run (#836)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.detections.reconstruction import (
    coverage_report,
    reconstruct,
    steps_from_dispatch_results,
)
from core.detections.tools import SecurityDetectionsTools

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_red_run.json"


def _recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_technique(report: dict) -> dict:
    return {row["technique_id"]: row for row in report["techniques"]}


def test_groups_reconstructed_steps_by_technique_id_join_by_step_id():
    recorded = _recorded()
    reconstructed = reconstruct(recorded["steps"], recorded["findings"])
    report = coverage_report(recorded["steps"], reconstructed)
    rows = _by_technique(report)

    assert rows["T1059.001"]["verdict"] == "both"
    assert rows["T1003.001"]["verdict"] == "missed"
    assert rows["T1021.002"]["verdict"] == "missed"
    assert rows["T1047"]["verdict"] == "loglm"

    missed = rows["T1003.001"]["missed"]
    assert len(missed) == 1
    assert missed[0]["id"] == "step-2"
    assert missed[0]["hostname"] == "dc01.corp.local"
    assert missed[0]["citations"] == []


def test_join_by_step_id_not_reconstructed_index():
    recorded = _recorded()
    reconstructed = reconstruct(recorded["steps"], recorded["findings"])
    shuffled = list(reversed(reconstructed["steps"]))
    for index, record in enumerate(shuffled):
        record["index"] = index
    report = coverage_report(recorded["steps"], {"steps": shuffled})
    missed = _by_technique(report)["T1003.001"]["missed"]
    assert missed[0]["id"] == "step-2"
    assert missed[0]["hostname"] == "dc01.corp.local"


def test_no_loglm_findings_never_emits_loglm_on_the_report():
    recorded = _recorded()
    findings = [
        finding for finding in recorded["findings"] if finding["data_source"] != "loglm"
    ]
    reconstructed = reconstruct(recorded["steps"], findings)
    report = coverage_report(recorded["steps"], reconstructed)
    verdicts = [row["verdict"] for row in report["techniques"]]

    assert "loglm" not in verdicts
    assert "both" not in verdicts
    rows = _by_technique(report)
    assert rows["T1059.001"]["verdict"] == "rule"
    assert rows["T1003.001"]["verdict"] == "missed"
    assert rows["T1047"]["verdict"] == "missed"


def test_steps_from_dispatch_results_unwrap_tool_result_rows():
    recorded = _recorded()
    results = [
        {
            "ok": True,
            "rows": recorded["steps"],
            "rowCount": len(recorded["steps"]),
            "capped": False,
            "sourceSystem": "vigil",
        }
    ]
    assert [step["id"] for step in steps_from_dispatch_results(results)] == [
        "step-1",
        "step-2",
        "step-3",
        "step-4",
    ]


def test_dispatch_rows_without_technique_id_are_not_trace_steps():
    recorded = _recorded()
    results = [
        {
            "ok": True,
            "rows": [
                {
                    "id": "siem-1",
                    "timestamp": "2020-01-01T00:00:00Z",
                    "hostname": "siem",
                },
                *recorded["steps"],
            ],
            "rowCount": 5,
            "capped": False,
            "sourceSystem": "splunk",
        }
    ]
    ids = [step["id"] for step in steps_from_dispatch_results(results)]
    assert "siem-1" not in ids
    assert ids == ["step-1", "step-2", "step-3", "step-4"]


@pytest.mark.asyncio
async def test_analyze_coverage_steps_is_the_run_report_not_catalog(monkeypatch):
    recorded = _recorded()

    class _Store:
        def get_findings(self, **_kwargs):
            return recorded["findings"]

    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())
    tools = SecurityDetectionsTools()
    report = await tools.analyze_coverage(steps=recorded["steps"], limit=2)

    assert "T1059.001" not in report
    rows = _by_technique(report)
    assert rows["T1059.001"]["verdict"] == "both"
    assert rows["T1003.001"]["verdict"] == "missed"
    assert rows["T1003.001"]["missed"][0]["id"] == "step-2"


@pytest.mark.asyncio
async def test_analyze_coverage_run_id_asks_the_agent_layer(monkeypatch):
    recorded = _recorded()
    seen: dict = {}

    async def fake_read(run_id: str):
        seen["run_id"] = run_id
        return {
            "run_id": run_id,
            "results": [
                {
                    "ok": True,
                    "rows": recorded["steps"],
                    "rowCount": len(recorded["steps"]),
                    "capped": False,
                    "sourceSystem": "vigil",
                }
            ],
        }

    class _Store:
        def get_findings(self, **_kwargs):
            return [
                finding
                for finding in recorded["findings"]
                if finding["data_source"] != "loglm"
            ]

    monkeypatch.setattr("core.detections.tools.read_projection", fake_read)
    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())

    tools = SecurityDetectionsTools()
    report = await tools.analyze_coverage(run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert seen["run_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    verdicts = [row["verdict"] for row in report["techniques"]]
    assert "loglm" not in verdicts
    assert "both" not in verdicts
    assert _by_technique(report)["T1059.001"]["verdict"] == "rule"


@pytest.mark.asyncio
async def test_empty_steps_with_run_id_still_reads_projection(monkeypatch):
    recorded = _recorded()
    seen: dict = {}

    async def fake_read(run_id: str):
        seen["run_id"] = run_id
        return {
            "run_id": run_id,
            "results": [
                {
                    "ok": True,
                    "rows": recorded["steps"],
                    "rowCount": len(recorded["steps"]),
                    "capped": False,
                    "sourceSystem": "vigil",
                }
            ],
        }

    class _Store:
        def get_findings(self, **_kwargs):
            return recorded["findings"]

    monkeypatch.setattr("core.detections.tools.read_projection", fake_read)
    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())
    report = await SecurityDetectionsTools().analyze_coverage(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", steps=[]
    )
    assert seen["run_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert _by_technique(report)["T1059.001"]["verdict"] == "both"


@pytest.mark.asyncio
async def test_analyze_coverage_techniques_only_stays_catalog():
    tools = SecurityDetectionsTools()
    tools._loaded = True
    tools.detections_by_technique["T1059.001"] = [
        {
            "title": "Encoded PowerShell",
            "_source": "sigma",
            "id": "sigma-1",
            "description": "catalog row",
        }
    ]
    result = await tools.analyze_coverage(techniques=["T1059.001"], limit=9)
    assert result["T1059.001"]["count"] == 1
    assert result["T1059.001"]["by_source"] == {"sigma": 1}
    assert "verdict" not in result["T1059.001"]
    assert "techniques" not in result

    mixed = await tools.analyze_coverage(techniques=["T1059.001"], steps=[])
    assert mixed["T1059.001"]["count"] == 1
    assert "verdict" not in mixed["T1059.001"]
    tools = SecurityDetectionsTools()
    tools._loaded = True
    tools.detections_by_technique["T1059.001"] = [
        {
            "title": "Encoded PowerShell",
            "_source": "sigma",
            "id": "sigma-1",
            "description": "catalog row",
        }
    ]
    result = await tools.analyze_coverage(techniques=["T1059.001"], limit=9)
    assert result["T1059.001"]["count"] == 1
    assert result["T1059.001"]["by_source"] == {"sigma": 1}
    assert "verdict" not in result["T1059.001"]
    assert "techniques" not in result
