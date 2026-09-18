# replay_hunt: one read of serve's Replay, offered to the backend registry and the
# findings MCP, declared in the schema, and granted to no builtin.

from __future__ import annotations

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS

pytestmark = pytest.mark.unit

REPORT = {"hunt_id": "wfr-hunt", "decisions": [], "recalled": []}


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
    assert result == REPORT
    assert asked == [("wfr-hunt", "dec-1")]


@pytest.mark.asyncio
async def test_backend_tool_answers_nothing_to_replay_with_an_error_body(monkeypatch):
    async def _read(run_id, decision_id=None):
        return None

    monkeypatch.setattr("core.agents.tool_registry.read_replay", _read)

    result, handled = await execute_backend_tool("replay_hunt", {"run_id": "wfr-x"})

    assert handled is True
    assert "error" in result


def test_schema_names_the_tool_and_no_builtin_is_granted_it():
    schema = next(tool for tool in ALL_TOOLS if tool["name"] == "replay_hunt")
    assert schema["input_schema"]["required"] == ["run_id"]
    assert "decision_id" in schema["input_schema"]["properties"]
    for agent in BUILTIN_AGENTS:
        granted = set(agent.get("tools", [])) | set(agent.get("recommended_tools", []))
        assert "replay_hunt" not in granted, agent["id"]
