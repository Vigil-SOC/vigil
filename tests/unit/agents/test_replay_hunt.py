# replay_hunt: one read of serve's Replay, offered to the backend registry and the
# findings MCP, declared in the schema, and granted to no builtin.

from __future__ import annotations

import json

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.mcp_tools import MCPFailure
from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS

pytestmark = pytest.mark.unit

REPORT = {"hunt_id": "wfr-hunt", "decisions": [], "recalled": []}


def _decision(n, **extra):
    return {
        "decision_id": f"dec-{n}",
        "iteration": n,
        "action": "DEEPEN",
        "target": "h-1",
        "cost_usd": 0.1,
        "exact": True,
        "mismatch": None,
        # What made the whole replay ~450K characters.
        "rebuilt": {"notes": ["n" * 5000]},
        "recorded": {"notes": ["n" * 5000]},
        **extra,
    }


def _big(steps=12):
    return {
        "hunt_id": "wfr-hunt",
        "decisions": [_decision(n) for n in range(1, steps + 1)],
        "recalled": ["r" * 900] * 9,
    }


def _stub(monkeypatch, report):
    async def _read(run_id, decision_id=None):
        return report

    monkeypatch.setattr("core.agents.tool_registry.read_replay", _read)


@pytest.mark.asyncio
async def test_backend_tool_dispatches_the_replay(monkeypatch):
    asked = []

    async def _read(run_id, decision_id=None):
        asked.append((run_id, decision_id))
        return REPORT

    monkeypatch.setattr("core.agents.tool_registry.read_replay", _read)

    result, handled = await execute_backend_tool(
        "replay_hunt", {"run_id": "wfr-hunt", "decision_id": "dec-1"}
    )

    assert handled is True
    assert result["hunt_id"] == "wfr-hunt" and result["steps"] == []
    assert asked == [("wfr-hunt", "dec-1")]


@pytest.mark.asyncio
async def test_backend_tool_answers_nothing_to_replay_with_an_error_body(monkeypatch):
    async def _read(run_id, decision_id=None):
        return None

    monkeypatch.setattr("core.agents.tool_registry.read_replay", _read)

    result, handled = await execute_backend_tool("replay_hunt", {"run_id": "wfr-x"})

    assert handled is True
    assert "error" in result


@pytest.mark.asyncio
async def test_a_step_answers_that_step_without_its_digests(monkeypatch):
    _stub(monkeypatch, _big())
    result, _ = await execute_backend_tool(
        "replay_hunt", {"run_id": "wfr-hunt", "step": 7}
    )
    assert [s["step"] for s in result["steps"]] == [7]
    assert result["steps"][0]["reproduced"] is True
    assert "rebuilt" not in result["steps"][0] and "recorded" not in result["steps"][0]
    assert result["steps_in_run"] == 12


@pytest.mark.asyncio
async def test_a_range_is_inclusive_and_capped_to_a_few_steps(monkeypatch):
    _stub(monkeypatch, _big())
    result, _ = await execute_backend_tool(
        "replay_hunt", {"run_id": "wfr-hunt", "from_step": 3, "to_step": 5}
    )
    assert [s["step"] for s in result["steps"]] == [3, 4, 5]
    wide, _ = await execute_backend_tool(
        "replay_hunt", {"run_id": "wfr-hunt", "from_step": 2}
    )
    assert [s["step"] for s in wide["steps"]] == [2, 3, 4, 5, 6]
    assert "6 more steps matched" in wide["omitted_steps"]


@pytest.mark.asyncio
async def test_no_step_answers_the_latest_steps_within_the_size_cap(monkeypatch):
    from core.agents import tool_registry

    report = _big(40)
    assert len(json.dumps(report)) > 400_000
    _stub(monkeypatch, report)
    result, _ = await execute_backend_tool("replay_hunt", {"run_id": "wfr-hunt"})
    assert [s["step"] for s in result["steps"]] == [36, 37, 38, 39, 40]
    assert len(result["recalled"]) == tool_registry.REPLAY_MAX_RECALLED
    assert result["omitted_recalled"] == 4
    assert len(json.dumps(result)) <= tool_registry.REPLAY_MAX_CHARS


@pytest.mark.asyncio
async def test_a_mismatch_is_reported_and_the_ceiling_sheds_whole_steps(monkeypatch):
    from core.agents import tool_registry

    report = {
        "hunt_id": "h",
        "decisions": [_decision(n, mismatch="m" * 5000) for n in range(1, 6)],
        "recalled": [],
    }
    _stub(monkeypatch, report)
    monkeypatch.setattr(tool_registry, "REPLAY_MAX_CHARS", 1_000)
    result, _ = await execute_backend_tool("replay_hunt", {"run_id": "h"})
    assert len(json.dumps(result)) <= 1_000
    assert result["steps"] and result["steps"][-1]["reproduced"] is False
    assert "output limit" in result["omitted_steps"]


@pytest.mark.asyncio
async def test_a_step_that_is_not_a_number_is_the_models_mistake_to_hear(monkeypatch):
    _stub(monkeypatch, _big())
    with pytest.raises(MCPFailure, match="step"):
        await execute_backend_tool("replay_hunt", {"run_id": "r", "step": "last"})


def test_schema_names_the_tool_and_no_builtin_is_granted_it():
    schema = next(tool for tool in ALL_TOOLS if tool["name"] == "replay_hunt")
    assert schema["input_schema"]["required"] == ["run_id"]
    props = schema["input_schema"]["properties"]
    assert {"decision_id", "step", "from_step", "to_step"} <= set(props)
    for agent in BUILTIN_AGENTS:
        granted = set(agent.get("tools", [])) | set(agent.get("recommended_tools", []))
        assert "replay_hunt" not in granted, agent["id"]
