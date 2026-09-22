"""Vigil's own tools, called directly instead of through a pipe.

Every other server in the registry is a program Vigil talks to: a vendor's, a
community one, something an operator installed. Vigil's own server was treated
the same way, which meant Vigil started a second copy of itself, held a pipe to
it, and serialised a call down that pipe to read its own database.

That cost more than the round trip. The child loads its own configuration, so
it can disagree with its parent; a crash in it arrives as a connection error
rather than a traceback; it needs connecting, enabling, reconnecting and a
status; and the caller's identity cannot cross a pipe, so work a person drove
was recorded as an agent's.

The tools are ordinary functions in this process. This calls them, and shapes
the answer the way the registry and the tool bridge already expect, so nothing
downstream has to know which side of the line a tool came from.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from core.integrations.mcp.surface import VIGIL_SERVER

logger = logging.getLogger(__name__)


def _server():
    """Vigil's MCP server object, imported late.

    ``tools.mcp.vigil`` pulls in the case and approval services; importing it
    when this module loads would drag them into anything that merely mentions
    the registry.
    """
    from tools.mcp.vigil import mcp

    return mcp


def list_tools() -> List[Dict[str, Any]]:
    """Vigil's tools, shaped the way ``MCPRegistry`` stores a connected server's.

    Read synchronously off the tool manager. The set is decided by decorators
    at import and does not change while the process runs, so there is nothing
    to await and no reason to make every caller of the registry async.
    """
    # The manager's Tool carries the JSON schema as ``parameters``; the
    # protocol Tool that a connected server would have sent calls the same
    # thing ``input_schema``, which is the name the registry stores.
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.parameters or {},
        }
        for tool in _server()._tool_manager.list_tools()
    ]


async def call_tool(
    name: str, args: Dict[str, Any], timeout: float = 30.0
) -> Dict[str, Any]:
    """Run one tool here, answering exactly as a call down the pipe answered.

    That contract, from ``MCPClient.call_tool``: never raise, always a dict,
    ``error`` says whether this failed and ``content`` carries the answer. The
    layer above reads ``result["error"]`` to decide whether to raise
    ``MCPFailure`` and ``result["content"]`` to read rows, so a direct call
    that raised, or that spelled the key differently, would turn a tool's own
    failure into a crash or into a silent success.

    The timeout is kept for the same reason. A call down a pipe was bounded;
    a call to a function is not bounded by anything unless it is made so, and
    a tool that never returns would otherwise hold its caller forever.
    """
    try:
        result = await asyncio.wait_for(
            _server().call_tool(name, args or {}), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.error("%s.%s timed out after %ss", VIGIL_SERVER, name, timeout)
        return {
            "error": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Tool call timed out after {timeout} seconds. "
                        "The MCP server may not be responding."
                    ),
                }
            ],
        }
    except Exception as e:  # noqa: BLE001 - the pipe turned these into results
        logger.error("Error calling %s.%s: %s", VIGIL_SERVER, name, e)
        return {"error": True, "content": [{"type": "text", "text": f"Error: {e}"}]}

    content = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            content.append({"type": "text", "text": text})
        elif hasattr(block, "type"):
            content.append({"type": str(block.type), "text": str(block)})
        else:
            content.append({"type": "text", "text": str(block)})

    return {"error": bool(getattr(result, "is_error", False)), "content": content}


def register(registry) -> int:
    """Put Vigil's own tools in the registry, without connecting to anything.

    The registry is how the rest of Vigil discovers which tools exist. Vigil's
    own belong in it for the same reason as everyone else's; that reaching them
    needs no connection is what the config here records.
    """
    try:
        tools = list_tools()
    except Exception:  # noqa: BLE001 - a registry short of tools beats no boot
        logger.exception("Could not list Vigil's own tools")
        return 0

    registry.register_server(VIGIL_SERVER, {"in_process": True}, tools=tools)
    return len(tools)
