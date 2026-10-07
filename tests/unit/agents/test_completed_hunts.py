# Completed-hunt pack: list_runs composed with read_projection, no agent_events fold.

from __future__ import annotations

from datetime import datetime

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.projections import pack_completed_hunts
from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS
from core.workflows.workflow_run_service import LIST_RUNS_MAX

pytestmark = pytest.mark.unit

START = "2026-01-01T00:00:00Z"
END = "2026-03-01T00:00:00Z"

PROJECTION = {
    "run_id": "wfr-kept",
    "status": "terminal",
    "hypotheses": [
        {
            "statement": "lateral SMB",
            "provenance": "caller",
            "status": "confirmed",
        }
    ],
    "evidence": [{"provenance": "splunk", "summary": "auth storm"}],
    "report": {
        "created_at": "2026-02-01T00:00:00Z",
        "terminated_at": "2026-02-01T01:00:00Z",
        "hypotheses": [{"statement": "lateral SMB", "status": "confirmed"}],
        "checkpoints": [
            {"resolution": {"actor": "analyst@soc", "decision": "approve"}}
        ],
    },
}


class _Runs:
    def __init__(self, runs, captured):
        self._runs = runs
        self._captured = captured

    def list_runs(self, **kwargs):
        self._captured.update(kwargs)
        return list(self._runs)


def _install(monkeypatch, runs, projections):
    captured = {}
    monkeypatch.setattr(
        "core.agents.projections.WorkflowRunService",
        lambda: _Runs(runs, captured),
    )

    async def _read(run_id):
        return projections.get(run_id)

    monkeypatch.setattr("core.agents.projections.read_projection", _read)
    return captured


@pytest.mark.asyncio
async def test_pack_returns_the_projection_unchanged(monkeypatch):
    _install(monkeypatch, [{"run_id": "wfr-kept"}], {"wfr-kept": PROJECTION})

    result = await pack_completed_hunts(start=START, end=END)

    assert result["hunts"] == [PROJECTION]
    assert result["start"] == START
    assert result["end"] == END


@pytest.mark.asyncio
async def test_pack_skips_a_run_whose_projection_is_missing(monkeypatch):
    _install(
        monkeypatch,
        [{"run_id": "wfr-gone"}, {"run_id": "wfr-kept"}],
        {"wfr-kept": PROJECTION},
    )

    result = await pack_completed_hunts(start=START, end=END)

    assert [hunt["run_id"] for hunt in result["hunts"]] == ["wfr-kept"]


@pytest.mark.asyncio
async def test_pack_asks_list_runs_for_completed_threat_hunts_in_the_window(
    monkeypatch,
):
    captured = _install(monkeypatch, [], {})

    await pack_completed_hunts(start=START, end=END, limit=500)

    assert captured["workflow_id"] == "threat-hunt"
    assert captured["status"] == "completed"
    assert captured["finished_after"] == datetime(2026, 1, 1, 0, 0, 0)
    assert captured["finished_at"] == datetime(2026, 3, 1, 0, 0, 0)
    assert captured["limit"] == LIST_RUNS_MAX


@pytest.mark.asyncio
async def test_pack_date_only_end_includes_that_utc_day(monkeypatch):
    captured = _install(monkeypatch, [], {})

    await pack_completed_hunts(start="2026-01-01", end="2026-03-31")

    assert captured["finished_after"] == datetime(2026, 1, 1, 0, 0, 0)
    assert captured["finished_at"] == datetime(2026, 3, 31, 23, 59, 59, 999999)


@pytest.mark.asyncio
async def test_pack_errors_when_every_projection_is_missing(monkeypatch):
    _install(monkeypatch, [{"run_id": "wfr-gone"}], {})

    with pytest.raises(RuntimeError, match="could not read hunt projections"):
        await pack_completed_hunts(start=START, end=END)


@pytest.mark.asyncio
async def test_pack_rejects_an_inverted_window():
    with pytest.raises(ValueError, match="start must be at or before end"):
        await pack_completed_hunts(start=END, end=START)


@pytest.mark.asyncio
async def test_backend_tool_dispatches_the_pack(monkeypatch):
    async def _pack(**kwargs):
        return {"start": kwargs["start"], "end": kwargs["end"], "hunts": [PROJECTION]}

    monkeypatch.setattr("core.agents.tool_registry.pack_completed_hunts", _pack)

    result, handled = await execute_backend_tool(
        "list_completed_hunts", {"start": START, "end": END}
    )

    assert handled is True
    assert result["hunts"] == [PROJECTION]


def test_schema_and_compliance_agent_name_the_tool():
    names = {tool["name"] for tool in ALL_TOOLS}
    assert "list_completed_hunts" in names
    compliance = next(row for row in BUILTIN_AGENTS if row["id"] == "compliance")
    assert "list_completed_hunts" in compliance["recommended_tools"]
