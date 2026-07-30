"""Single path through which Vigil reaches MCP servers (TDD 8.8, epic #214).

Owns tool-name resolution (``<server>_<tool>`` prefix or whole-name lookup) so
callers stop duplicating it, and carries per-call provenance in
``GatewayContext``.

ponytail: pass-through only — turn-scoped dedup (#266), reactive size
enforcement (#267), source_ref rewriting (#268) and per-call OTEL (#269) belong
in ``call_tool`` below, and transport moves to mcp-proxy under #265.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class GatewayContext:
    """Who is calling: the agent, and the investigation/turn it is working on."""

    agent: str
    investigation_id: Optional[str] = None
    turn: Optional[int] = None


def _resolve(client: Any, tool_name: str) -> Tuple[Optional[str], str]:
    """Map a flat tool name onto ``(server_name, tool_name)``.

    Accepts a ``<server>_<tool>`` prefix when that server is configured — the
    tools cache is cold until first connect, so a configured-but-unconnected
    server must still resolve — then falls back to scanning the cache for a
    tool whose bare name matches.
    """
    cache = client.tools_cache or {}
    servers = getattr(client.mcp_service, "servers", None) or {}

    if "_" in tool_name:
        prefix, suffix = tool_name.split("_", 1)
        if prefix in cache or prefix in servers:
            return prefix, suffix

    for server_name, tools in cache.items():
        if any(t.get("name") == tool_name for t in tools):
            return server_name, tool_name

    return None, tool_name


def resolve_tool(tool_name: str) -> Tuple[Optional[str], str]:
    """Public resolution for callers that need the server/tool names themselves.

    Chat surfaces label ``prompt_security.wrap_tool_result`` with the resolved
    server, so they need the split before the result comes back.
    """
    from services.mcp_client import get_mcp_client

    client = get_mcp_client()
    if client is None:
        return None, tool_name
    return _resolve(client, tool_name)


async def call_tool(
    ctx: GatewayContext,
    tool_name: str,
    arguments: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Optional[Dict[str, Any]]:
    """Execute an MCP tool call. Returns None when the tool cannot be routed."""
    from services.mcp_client import get_mcp_client

    client = get_mcp_client()
    if client is None:
        logger.warning("MCP client unavailable; cannot route %s", tool_name)
        return None

    server_name, actual_tool_name = _resolve(client, tool_name)
    if server_name is None:
        logger.warning(
            "No MCP server resolves tool %s (agent=%s investigation=%s)",
            tool_name,
            ctx.agent,
            ctx.investigation_id,
        )
        return None

    logger.info(
        "gateway: %s -> %s.%s (agent=%s investigation=%s turn=%s)",
        tool_name,
        server_name,
        actual_tool_name,
        ctx.agent,
        ctx.investigation_id,
        ctx.turn,
    )
    return await client.call_tool(
        server_name, actual_tool_name, arguments, timeout=timeout
    )
