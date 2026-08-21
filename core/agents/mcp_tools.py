# The other half of the tool bridge. execute_backend_tool answers for the tools
# this process implements; this answers for the forty MCP servers, which nothing
# in the agent layer could reach before.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# What a failure here is, in the taxonomy contracts/tool.ts declares. unavailable
# is a visibility gap the hunt reasons over; backend_error is a defect. Keeping
# them apart is the whole reason this returns a kind rather than a string.
if TYPE_CHECKING:
    from core.integrations.mcp.registry import MCPRegistry

UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
BACKEND_ERROR = "backend_error"


class MCPFailure(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# Tool names arrive flattened as {server}_{tool}, which registry.get_all_tools
# builds. Server names carry both hyphens and underscores -- splunk-selfhosted,
# gcp-secops -- so the prefix is matched against the registered names, longest
# first, rather than split on the separator.
def split_tool_name(flat: str, servers: List[str]) -> Optional[Tuple[str, str]]:
    for server in sorted(servers, key=len, reverse=True):
        prefix = f"{server}_"
        if flat.startswith(prefix) and len(flat) > len(prefix):
            return server, flat[len(prefix) :]
    return None


# The MCP content block, as rows. A server answers with text parts; each becomes
# one row, and JSON text is parsed so a caller reads records rather than a string.
def rows_from(result: Dict[str, Any]) -> List[Any]:
    import json

    content = result.get("content")
    if not isinstance(content, list):
        return [result]

    rows: List[Any] = []
    for part in content:
        if not isinstance(part, dict):
            rows.append(part)
            continue
        text = part.get("text")
        if text is None:
            rows.append(part)
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            rows.append({"text": text})
            continue
        rows.extend(parsed) if isinstance(parsed, list) else rows.append(parsed)
    return rows


# A server that answered with an error says which kind it was. "Unknown server"
# and a failed connect are gaps in what could be reached; the rest are defects.
def _failure_kind(text: str) -> str:
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return TIMEOUT
    if "unknown server" in lowered or "failed to connect" in lowered:
        return UNAVAILABLE
    return BACKEND_ERROR


def _text_of(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("text")
        ]
        if parts:
            return " ".join(parts)
    error = result.get("error")
    return error if isinstance(error, str) else "the server reported an error"


# Returns (rows, handled). handled is False only when no active server carries
# the name, which is the caller's cue to report it as the defect it is -- a tool
# nothing implements is not a gap in visibility.
async def execute_mcp_tool(
    tool_name: str, args: Dict[str, Any], timeout_s: float, registry: "MCPRegistry"
) -> Tuple[Any, bool]:
    servers = registry.get_active_servers()
    if not servers:
        return None, False

    split = split_tool_name(tool_name, servers)
    if split is None:
        return None, False
    server, tool = split

    # Checked against what the server actually reports, so a name that merely
    # looks right does not open a call the far side will refuse.
    if tool_name not in registry.get_tool_names():
        return None, False

    from core.integrations.mcp.client import process_mcp_client

    client = process_mcp_client()
    if client is None:
        raise MCPFailure(
            UNAVAILABLE, f"{server} is configured but no client is running"
        )

    result = await client.call_tool(server, tool, args, timeout=timeout_s)
    if not isinstance(result, dict):
        return [result], True
    if result.get("error"):
        detail = _text_of(result)
        raise MCPFailure(_failure_kind(detail), detail)

    return rows_from(result), True
