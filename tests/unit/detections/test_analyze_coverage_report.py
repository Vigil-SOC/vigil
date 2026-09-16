"""Run-scoped coverage report from reconstruct_run (#836)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.integrations.atomic_red_team.tool as art
from core.detections.reconstruction import (
    coverage_report,
    reconstruct,
    steps_from_dispatch_results,
)
from core.detections.tools import SecurityDetectionsTools
from tests.unit.detections.fixtures.art_trace import CONFIG, stub_run

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_red_run.json"


def _recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_technique(report: dict) -> dict:
    return {row["technique_id"]: row for row in report["techniques"]}


def _envelope(rows: list) -> dict:
    return {
        "ok": True,
        "rows": rows,
        "rowCount": len(rows),
        "capped": False,
        "sourceSystem": "vigil",
    }


def _finding(finding_id: str, data_source: str, hostname: str) -> dict:
    return {
        "finding_id": finding_id,
        "data_source": data_source,
        "timestamp": "2026-09-10T12:01:00Z",
        "entity_context": {"hostnames": [hostname.upper()]},
    }


def _execute(monkeypatch, hostname: str = "ws01.corp.local") -> dict:
    clock = iter(["2026-09-10T12:00:00+00:00", "2026-09-10T12:05:00+00:00"])
    monkeypatch.setattr(art, "_now", lambda: next(clock))
    return art.execute_atomic(
        {"technique": "T1059.001", "environment_id": "range-1", "hostname": hostname},
        CONFIG,
        run=stub_run,
    )


def test_execute_atomic_trace_scores_end_to_end(monkeypatch):
    # The seam the issue is about: tool output -> journaled rows -> steps ->
    # verdict -> technique row, with no translation in between.
    out = _execute(monkeypatch)
    trace = steps_from_dispatch_results([_envelope([out])])
    assert trace == [out]
    assert steps_from_dispatch_results([out]) == [out]

    findings = [
        _finding("elastic-enc-ps", "elastic", out["hostname"]),
        _finding("loglm-seq", "loglm", out["hostname"]),
    ]
    reconstructed = reconstruct(trace, findings[:1])
    assert reconstructed["steps"][0]["verdict"] == "rule"
    assert reconstruct(trace, findings[1:])["steps"][0]["verdict"] == "loglm"

    row = _by_technique(coverage_report(trace, reconstruct(trace, findings)))
    assert row["T1059.001"]["verdict"] == "both"
    assert row["T1059.001"]["missed"] == []


def test_execute_atomic_trace_with_no_matching_finding_is_missed(monkeypatch):
    out = _execute(monkeypatch, hostname="dc01.corp.local")
    trace = steps_from_dispatch_results([_envelope([out])])
    findings = [_finding("elastic-elsewhere", "elastic", "ws01.corp.local")]
    report = coverage_report(trace, reconstruct(trace, findings))
    row = _by_technique(report)["T1059.001"]
    assert row["verdict"] == "missed"
    assert row["missed"][0]["hostname"] == "dc01.corp.local"
    assert row["missed"][0]["index"] == 0


def test_groups_reconstructed_steps_by_technique_id():
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
    assert missed[0]["index"] == 1
    assert missed[0]["hostname"] == "dc01.corp.local"
    assert missed[0]["citations"] == []


def test_duplicate_step_ids_join_in_order_not_last_write():
    trace = [
        {"id": "s1", "technique_id": "T1003.001", "hostname": "dc01.corp.local"},
        {"id": "s1", "technique_id": "T1003.001", "hostname": "dc02.corp.local"},
    ]
    reconstructed = {
        "steps": [
            {"id": "s1", "index": 0, "verdict": "missed", "citations": []},
            {
                "id": "s1",
                "index": 1,
                "verdict": "rule",
                "citations": [{"finding_id": "elastic-lsass"}],
            },
        ]
    }
    report = coverage_report(trace, reconstructed)
    row = _by_technique(report)["T1003.001"]
    assert [step["verdict"] for step in row["steps"]] == ["missed", "rule"]
    assert row["verdict"] == "rule"
    assert len(row["missed"]) == 1
    assert row["missed"][0]["hostname"] == "dc01.corp.local"
    assert row["missed"][0]["index"] == 0


def test_join_by_step_id_not_reconstructed_index():
    trace = [
        {"id": "step-1", "technique_id": "T1059.001", "hostname": "ws01.corp.local"},
        {"id": "step-2", "technique_id": "T1003.001", "hostname": "dc01.corp.local"},
    ]
    # Records arrive in the opposite order from the trace; the id must win.
    shuffled = [
        {"id": "step-2", "index": 0, "verdict": "missed", "citations": []},
        {
            "id": "step-1",
            "index": 1,
            "verdict": "rule",
            "citations": [{"finding_id": "elastic-enc-ps"}],
        },
    ]
    report = coverage_report(trace, {"steps": shuffled})
    rows = _by_technique(report)
    assert rows["T1059.001"]["verdict"] == "rule"
    missed = rows["T1003.001"]["missed"]
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


TECHNIQUES = ["T1059.001", "T1003.001", "T1021.002", "T1047"]


def test_steps_from_dispatch_results_unwrap_tool_result_rows():
    recorded = _recorded()
    steps = steps_from_dispatch_results([_envelope(recorded["steps"])])
    assert [step["technique_id"] for step in steps] == TECHNIQUES


def test_dispatch_rows_without_technique_id_are_not_trace_steps():
    recorded = _recorded()
    siem_row = {"id": "siem-1", "timestamp": "2020-01-01T00:00:00Z", "hostname": "siem"}
    results = [{**_envelope([siem_row, *recorded["steps"]]), "sourceSystem": "splunk"}]
    steps = steps_from_dispatch_results(results)
    assert all("id" not in step for step in steps)
    assert [step["technique_id"] for step in steps] == TECHNIQUES


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
    assert rows["T1003.001"]["missed"][0]["hostname"] == "dc01.corp.local"


@pytest.mark.asyncio
async def test_analyze_coverage_run_id_asks_the_agent_layer(monkeypatch):
    recorded = _recorded()
    seen: dict = {}

    async def fake_read(run_id: str):
        seen["run_id"] = run_id
        return {"run_id": run_id, "results": [_envelope(recorded["steps"])]}

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
        return {"run_id": run_id, "results": [_envelope(recorded["steps"])]}

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
