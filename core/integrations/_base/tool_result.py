"""Shared ``CallToolResult`` builder for the in-repo MCP tool servers.

Handlers report a handled failure as a JSON payload with a truthy top-level
``error`` key (``core/agents/mcp_tools.py`` reads the same contract). Mapping
that to ``is_error`` here keeps the flag honest for every caller of the server.
"""

import json
from typing import Awaitable, Callable, Optional

import mcp.types as types


def payload_is_error(content) -> bool:
    """True when any text block is a JSON object with a truthy ``error`` key."""
    for block in content or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("error"):
            return True
    return False


async def run_tool(
    handler: Callable[[str, Optional[dict]], Awaitable[list]], params
) -> types.CallToolResult:
    """Run ``handler`` for an ``on_call_tool`` request and shape its answer."""
    try:
        content = await handler(params.name, params.arguments)
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))],
            is_error=True,
        )
    return types.CallToolResult(content=content, is_error=payload_is_error(content))
