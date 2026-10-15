"""A persistent MCP session outlives the task that opened it.

The enable route connects from inside an HTTP request. The stdio child and its
session must not belong to that request: when the request's task group exits
(or the browser gives up and the request is cancelled), the connection has to
stay up, and nothing may raise on the way out.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import anyio
import pytest
from mcp import StdioServerParameters

from core.integrations.mcp.client import PersistentServerSession

pytestmark = pytest.mark.unit

SERVER = textwrap.dedent(
    """
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("echo")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    mcp.run()
    """
)


@pytest.fixture
def params(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(SERVER)
    return StdioServerParameters(command=sys.executable, args=[str(script)])


async def _echo(holder: PersistentServerSession) -> str:
    result = await holder.call_tool("echo", {"text": "still here"})
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_connect_inside_a_task_group_that_then_exits(params):
    """What BaseHTTPMiddleware does around a request handler."""
    holder = PersistentServerSession("echo", params)

    async with anyio.create_task_group() as tg:
        tg.start_soon(holder.connect)

    try:
        assert holder.is_connected
        assert await _echo(holder) == "still here"
    finally:
        await holder.disconnect()
    assert not holder.is_connected


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_take_the_connection_down(params):
    """The browser gave up on the enable request while the child was starting."""
    holder = PersistentServerSession("echo", params)

    caller = asyncio.create_task(holder.connect())
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    try:
        # A second enable waits for the first start rather than racing it.
        assert await holder.connect()
        assert await _echo(holder) == "still here"
    finally:
        await holder.disconnect()


@pytest.mark.asyncio
async def test_a_child_that_cannot_start_reports_why(tmp_path):
    holder = PersistentServerSession(
        "broken",
        StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(3)"]),
    )
    assert not await holder.connect()
    assert holder.last_error
    assert not holder.is_connected
