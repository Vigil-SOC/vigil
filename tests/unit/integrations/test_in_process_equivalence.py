"""Calling Vigil's own tools in this process answers as the pipe answered.

Vigil used to reach its own tools by starting a second copy of itself and
speaking down a pipe to it. Removing that is only safe if the short way is
indistinguishable to everything downstream, so the contract the pipe kept is
written out here and held against the direct path.

The contract, read off ``MCPClient.call_tool``:

* It never raises. Every outcome is a dict.
* Success is ``{"error": <is_error>, "content": [...]}``.
* A timeout is ``{"error": True, ...}`` saying so, after the caller's bound.
* Any other failure is ``{"error": True, "content": [{"type": "text",
  "text": "Error: ..."}]}``.

``execute_mcp_tool`` reads ``result["error"]`` to decide whether to raise
``MCPFailure``, and ``rows_from`` reads ``result["content"]``. A direct call
that raised, or that spelled the key differently, would turn a tool's own
error into a crash or a silence.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer

from core.integrations.mcp import in_process


@pytest.fixture
def server(monkeypatch):
    """A stand-in for Vigil's server, so these do not touch a database."""
    probe = MCPServer("vigil")

    @probe.tool()
    def answers(marker: str = "x") -> str:
        return '{"ok": true}'

    @probe.tool()
    def raises(marker: str = "x") -> str:
        raise ValueError("the tool itself failed")

    @probe.tool()
    def reports_its_own_error(marker: str = "x") -> str:
        # What Vigil's tools do: catch, and answer with an error payload.
        return '{"error": "something went wrong"}'

    @probe.tool()
    def never_returns(marker: str = "x") -> str:
        import time

        time.sleep(5)
        return "too late"

    monkeypatch.setattr(in_process, "_server", lambda: probe)
    return probe


def call(name, args=None, timeout=30.0):
    return asyncio.run(in_process.call_tool(name, args or {"marker": "x"}, timeout))


def test_a_working_tool_answers_in_the_shape_the_pipe_used(server):
    result = call("answers")

    assert isinstance(result, dict)
    assert result["error"] is False
    assert result["content"] == [{"type": "text", "text": '{"ok": true}'}]


def test_a_tool_that_raises_does_not_raise_at_the_caller(server):
    """The pipe turned a far-side exception into an error result. So does this."""
    result = call("raises")

    assert isinstance(result, dict)
    assert result["error"] is True
    assert "Error:" in result["content"][0]["text"]


def test_a_tool_that_reports_its_own_error_is_not_an_error_result(server):
    """`{"error": ...}` in the payload is the tool speaking, not the transport."""
    result = call("reports_its_own_error")

    assert result["error"] is False
    assert "something went wrong" in result["content"][0]["text"]


def test_an_unknown_tool_is_an_error_result_rather_than_an_exception(server):
    result = call("no_such_tool")

    assert result["error"] is True
    assert "Error:" in result["content"][0]["text"]


def test_a_call_is_bounded_by_its_timeout(server):
    """Without this a direct call has no bound at all; the pipe always had one."""
    result = call("never_returns", timeout=0.2)

    assert result["error"] is True
    assert "timed out" in result["content"][0]["text"]


def test_the_error_key_is_the_one_execute_mcp_tool_reads(server):
    """`isError` would be silently falsy and a failure would read as success."""
    result = call("raises")

    assert "error" in result
    assert "isError" not in result


def test_a_failure_reaches_execute_mcp_tool_as_a_failure(server):
    """The whole point: the layer above behaves the same either way."""
    from core.agents.mcp_tools import MCPFailure, _text_of

    result = call("raises")

    assert result.get("error")
    detail = _text_of(result)
    assert detail
    # The caller raises this; here we only prove it has what it needs to.
    assert isinstance(MCPFailure("unavailable", detail), Exception)


def test_rows_come_back_the_way_the_bridge_parses_them(server):
    from core.agents.mcp_tools import rows_from

    assert rows_from(call("answers")) == [{"ok": True}]


def test_the_tool_list_is_shaped_like_a_registered_server(server):
    tools = in_process.list_tools()

    assert {t["name"] for t in tools} >= {"answers", "raises"}
    for tool in tools:
        assert set(tool) == {"name", "description", "input_schema"}
