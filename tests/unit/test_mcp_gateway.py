"""Tests for ``services.mcp_gateway`` — the single path to MCP servers (#270).

The bug this file exists to prevent: ``MCPClient.call_tool`` takes
``(server_name, tool_name, arguments)``, and two daemon call sites passed
``(tool_name, arguments)`` instead, so approved containment and case linking
silently resolved to "Unknown server". Resolution now lives in one place.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.mcp_gateway import GatewayContext, _resolve, call_tool  # noqa: E402

pytestmark = pytest.mark.unit


def _client(*, cache=None, servers=None):
    client = MagicMock()
    client.tools_cache = cache if cache is not None else {}
    client.mcp_service.servers = servers if servers is not None else {}
    client.call_tool = AsyncMock(return_value={"error": False, "content": []})
    return client


def test_resolve_prefix_from_tools_cache():
    client = _client(cache={"splunk": [{"name": "nl_search"}]})
    assert _resolve(client, "splunk_nl_search") == ("splunk", "nl_search")


def test_resolve_prefix_from_configured_server_with_cold_cache():
    """A configured server resolves before its tools cache is warm."""
    client = _client(servers={"splunk": object()})
    assert _resolve(client, "splunk_nl_search") == ("splunk", "nl_search")


def test_resolve_scans_cache_for_unprefixed_name():
    client = _client(cache={"cases": [{"name": "link_related_cases"}]})
    assert _resolve(client, "link_related_cases") == ("cases", "link_related_cases")


def test_resolve_returns_none_when_unknown():
    assert _resolve(_client(), "nope_at_all") == (None, "nope_at_all")


@pytest.mark.asyncio
async def test_call_tool_forwards_server_and_tool_separately():
    client = _client(cache={"splunk": [{"name": "nl_search"}]})
    with patch("services.mcp_client.get_mcp_client", return_value=client):
        result = await call_tool(
            GatewayContext("agent_runner", "inv1", 3), "splunk_nl_search", {"q": "x"}
        )
    client.call_tool.assert_awaited_once_with(
        "splunk", "nl_search", {"q": "x"}, timeout=30.0
    )
    assert result == {"error": False, "content": []}


@pytest.mark.asyncio
async def test_call_tool_returns_none_without_calling_when_unresolvable():
    client = _client()
    with patch("services.mcp_client.get_mcp_client", return_value=client):
        assert await call_tool(GatewayContext("agent_runner"), "nope", {}) is None
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_tool_returns_none_without_client():
    with patch("services.mcp_client.get_mcp_client", return_value=None):
        assert await call_tool(GatewayContext("orchestrator"), "splunk_x", {}) is None


def _runner():
    """An AgentRunner with no Claude service, so MCP tools take the gateway leg."""
    from daemon.agent_runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner._claude_service = None
    runner.workdir = MagicMock()
    runner.config = MagicMock()
    runner.config.dry_run = False
    return runner


@pytest.mark.asyncio
async def test_agent_loop_routes_mcp_tools_through_gateway():
    gw = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    with patch("daemon.agent_runner._get_tool_tier", return_value="auto"), patch(
        "services.mcp_gateway.call_tool", gw
    ):
        result = await _runner()._execute_tool(
            "inv1", "splunk_nl_search", {"q": "x"}, 3
        )

    ctx, tool_name, arguments = gw.await_args.args
    assert (ctx.agent, ctx.investigation_id, ctx.turn) == ("agent_runner", "inv1", 3)
    assert (tool_name, arguments) == ("splunk_nl_search", {"q": "x"})
    assert "ok" in result


@pytest.mark.asyncio
async def test_approved_tool_routes_through_gateway_with_investigation():
    gw = AsyncMock(return_value={"content": []})
    with patch("services.mcp_gateway.call_tool", gw):
        await _runner()._execute_approved_tool("isolate_host", {"host": "h1"}, "inv2")

    ctx, tool_name, _ = gw.await_args.args
    assert (ctx.agent, ctx.investigation_id) == ("agent_runner_approved", "inv2")
    assert tool_name == "isolate_host"


@pytest.mark.asyncio
async def test_chat_surface_routes_through_gateway_and_keeps_security_wrapper():
    """The one chat MCP surface still truncates and frames results as data (#87)."""
    from services.chat.tool_executor import ToolExecutor

    block = [{"type": "tool_use", "id": "t1", "name": "splunk_nl_search", "input": {}}]
    with patch(
        "services.mcp_gateway.resolve_tool", return_value=("splunk", "nl_search")
    ), patch(
        "services.chat.tool_executor.call_tool",
        AsyncMock(return_value={"content": [{"type": "text", "text": "hits"}]}),
    ):
        results = await ToolExecutor().process_mcp_tool_use(block)

    text = results[0]["content"][0]["text"]
    assert results[0]["tool_use_id"] == "t1"
    assert "hits" in text and "vigil:tool_result" in text


@pytest.mark.asyncio
async def test_claude_service_mcp_dispatch_delegates_to_the_one_surface():
    """_process_tool_use is a shim over ToolExecutor — no second copy to drift."""
    from services.claude_service import ClaudeService

    service = object.__new__(ClaudeService)
    service._tool_executor = MagicMock()
    service._tool_executor.process_mcp_tool_use = AsyncMock(return_value=["ok"])

    assert await service._process_tool_use(["block"]) == ["ok"]
    service._tool_executor.process_mcp_tool_use.assert_awaited_once_with(["block"])


@pytest.mark.asyncio
async def test_openai_surface_routes_through_gateway():
    from services.openai_agent_service import OpenAIAgentService

    agent = object.__new__(OpenAIAgentService)
    with patch(
        "services.mcp_gateway.resolve_tool", return_value=("splunk", "nl_search")
    ), patch(
        "services.mcp_gateway.call_tool",
        AsyncMock(return_value={"content": [{"type": "text", "text": "hits"}]}),
    ):
        text, is_error = await agent._execute_mcp_tool("splunk_nl_search", {})

    assert is_error is False
    assert "hits" in text and "vigil:tool_result" in text


@pytest.mark.asyncio
async def test_openai_surface_reports_unroutable_tool_as_error():
    from services.openai_agent_service import OpenAIAgentService

    agent = object.__new__(OpenAIAgentService)
    with patch("services.mcp_gateway.resolve_tool", return_value=(None, "nope")):
        text, is_error = await agent._execute_mcp_tool("nope", {})

    assert is_error is True
    assert "No MCP server found" in text
