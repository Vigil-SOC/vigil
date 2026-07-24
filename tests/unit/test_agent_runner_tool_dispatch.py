"""Backend-tool dispatch routing for ``daemon.agent_runner.AgentRunner`` (#393).

After the tool-dispatch consolidation the daemon no longer reaches into the
private ``ClaudeService._execute_backend_tool``; both tool-execution sites route
built-in tools through ``services.tool_manager.execute_backend_tool`` (which
returns ``(result, handled)``) and fall back to the daemon's own MCP client when
the tool is not a backend tool (``handled is False``).

These tests lock that routing for both call sites:

  * ``_execute_approved_tool``  — the post-approval, guardrail-bypassing path.
  * ``_execute_external_tool``  — the guarded path (tier gate runs first).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


def _runner():
    """Build an AgentRunner with a stubbed config + workdir (no agent loop)."""
    from daemon.agent_runner import AgentRunner
    from daemon.config import OrchestratorConfig

    cfg = OrchestratorConfig()
    cfg.dry_run = False

    workdir = MagicMock()
    workdir.append_log = MagicMock()

    runner = AgentRunner(cfg, workdir)
    # skill_index is read off the claude_service instance via getattr.
    runner._claude_service = MagicMock(_skill_tool_index=None)
    return runner


def test_approved_tool_routes_backend_via_tool_manager():
    """A backend tool is dispatched through tool_manager; MCP is not touched."""
    runner = _runner()
    with patch(
        "daemon.agent_runner.execute_backend_tool",
        new=AsyncMock(return_value=({"ok": 1}, True)),
    ) as m_backend, patch("services.mcp_client.get_mcp_client") as m_mcp:
        result = asyncio.run(
            runner._execute_approved_tool("list_findings", {"limit": 5})
        )

    assert json.loads(result) == {"ok": 1}
    m_backend.assert_awaited_once()
    m_mcp.assert_not_called()


def test_approved_tool_falls_back_to_mcp_when_not_handled():
    """A non-backend tool (handled=False) falls through to the MCP client."""
    runner = _runner()
    client = MagicMock()
    client.call_tool = AsyncMock(return_value={"mcp": "yes"})
    with patch(
        "daemon.agent_runner.execute_backend_tool",
        new=AsyncMock(return_value=(None, False)),
    ) as m_backend, patch("services.mcp_client.get_mcp_client", return_value=client):
        result = asyncio.run(runner._execute_approved_tool("splunk_search", {}))

    assert json.loads(result) == {"mcp": "yes"}
    m_backend.assert_awaited_once()
    client.call_tool.assert_awaited_once()


def test_external_tool_routes_backend_via_tool_manager():
    """The guarded path also dispatches backend tools through tool_manager."""
    runner = _runner()
    with patch("daemon.agent_runner._get_tool_tier", return_value="read"), patch(
        "daemon.agent_runner.execute_backend_tool",
        new=AsyncMock(return_value=({"ok": 2}, True)),
    ) as m_backend, patch("services.mcp_client.get_mcp_client") as m_mcp:
        result = asyncio.run(
            runner._execute_external_tool("inv-1", "list_findings", {"limit": 5})
        )

    assert json.loads(result) == {"ok": 2}
    m_backend.assert_awaited_once()
    m_mcp.assert_not_called()


def test_external_tool_falls_back_to_mcp_when_not_handled():
    """The guarded path falls through to MCP for non-backend tools."""
    runner = _runner()
    client = MagicMock()
    client.tools_cache = {"splunk": [{"name": "splunk_search"}]}
    client.call_tool = AsyncMock(return_value={"mcp": "ok"})
    with patch("daemon.agent_runner._get_tool_tier", return_value="read"), patch(
        "daemon.agent_runner.execute_backend_tool",
        new=AsyncMock(return_value=(None, False)),
    ) as m_backend, patch("services.mcp_client.get_mcp_client", return_value=client):
        result = asyncio.run(
            runner._execute_external_tool("inv-1", "splunk_search", {})
        )

    assert json.loads(result) == {"mcp": "ok"}
    m_backend.assert_awaited_once()
    client.call_tool.assert_awaited_once()
