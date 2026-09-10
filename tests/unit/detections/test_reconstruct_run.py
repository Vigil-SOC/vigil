"""Per-step reconstruction of a red run (#835)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.detections.reconstruction import reconstruct
from core.detections.tools import SecurityDetectionsTools

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_red_run.json"


def _recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _verdicts(result: dict) -> list[str]:
    return [step["verdict"] for step in result["steps"]]


def test_recorded_run_missed_when_window_has_no_entity_time_overlap():
    recorded = _recorded()
    result = reconstruct(recorded["steps"], recorded["findings"])

    by_id = {step["id"]: step for step in result["steps"]}
    missed = by_id["step-2"]
    assert missed["verdict"] == "missed"
    assert missed["citations"] == []

    empty_window = by_id["step-3"]
    assert empty_window["verdict"] == "missed"
    assert empty_window["citations"] == []


def test_recorded_run_loglm_when_only_loglm_hits_the_step():
    recorded = _recorded()
    result = reconstruct(recorded["steps"], recorded["findings"])
    step = next(item for item in result["steps"] if item["id"] == "step-4")
    assert step["verdict"] == "loglm"
    assert [item["finding_id"] for item in step["citations"]] == ["loglm-wmi-seq"]


def test_recorded_run_both_when_rule_and_loglm_hit_the_same_step():
    recorded = _recorded()
    result = reconstruct(recorded["steps"], recorded["findings"])

    step = next(item for item in result["steps"] if item["id"] == "step-1")
    assert step["verdict"] == "both"
    cited = {item["finding_id"]: item for item in step["citations"]}
    assert set(cited) == {"elastic-enc-ps", "loglm-seq-1"}
    assert cited["elastic-enc-ps"]["description"] == "Encoded PowerShell command line"
    assert cited["elastic-enc-ps"]["rule_name"] == "Encoded PowerShell"
    assert cited["loglm-seq-1"]["description"] == "Anomalous process sequence"


def test_no_loglm_findings_never_emits_loglm():
    recorded = _recorded()
    findings = [
        finding for finding in recorded["findings"] if finding["data_source"] != "loglm"
    ]
    result = reconstruct(recorded["steps"], findings)

    assert "loglm" not in _verdicts(result)
    assert "both" not in _verdicts(result)
    by_id = {step["id"]: step for step in result["steps"]}
    assert by_id["step-1"]["verdict"] == "rule"
    assert by_id["step-2"]["verdict"] == "missed"
    assert by_id["step-4"]["verdict"] == "missed"


def test_unknown_step_keys_are_ignored_and_do_not_join_on_technique_id():
    recorded = _recorded()
    result = reconstruct(recorded["steps"], recorded["findings"])
    step = next(item for item in result["steps"] if item["id"] == "step-2")
    assert step["verdict"] == "missed"


@pytest.mark.asyncio
async def test_backend_tool_dispatches_reconstruct_run(monkeypatch):
    from core.agents.tool_registry import execute_backend_tool
    from core.llm.tool_schemas import ALL_TOOLS

    recorded = _recorded()

    class _Store:
        def get_findings(self, **_kwargs):
            return recorded["findings"]

    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())

    assert any(tool["name"] == "reconstruct_run" for tool in ALL_TOOLS)

    result, handled = await execute_backend_tool(
        "reconstruct_run",
        {"steps": recorded["steps"], "limit": 2, "technique_id": "T1059.001"},
    )
    assert handled is True
    assert _verdicts(result) == ["both", "missed", "missed", "loglm"]

    no_loglm = [
        finding for finding in recorded["findings"] if finding["data_source"] != "loglm"
    ]

    class _RulesOnly:
        def get_findings(self, **_kwargs):
            return no_loglm

    monkeypatch.setattr(
        "core.detections.tools.DatabaseDataService", lambda: _RulesOnly()
    )
    result, handled = await execute_backend_tool(
        "reconstruct_run", {"steps": recorded["steps"]}
    )
    assert handled is True
    assert "loglm" not in _verdicts(result)
    assert "both" not in _verdicts(result)

    tools = SecurityDetectionsTools()
    via_tools = await tools.reconstruct_run(steps=recorded["steps"])
    assert _verdicts(via_tools) == ["rule", "missed", "missed", "missed"]


@pytest.mark.asyncio
async def test_tool_reads_findings_in_the_trace_window(monkeypatch):
    recorded = _recorded()
    seen: dict = {}

    class _Store:
        def get_findings(self, **kwargs):
            seen.update(kwargs)
            return recorded["findings"]

    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())
    await SecurityDetectionsTools().reconstruct_run(steps=recorded["steps"])
    assert seen["timestamp_start"] == datetime(2026, 9, 10, 12, 0, 0)
    assert seen["timestamp_end"] == datetime(2026, 9, 10, 12, 22, 0)
