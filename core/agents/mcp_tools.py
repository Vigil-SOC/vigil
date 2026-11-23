# The other half of the tool bridge. execute_backend_tool answers for the tools
# this process implements; this answers for the forty MCP servers, which nothing
# in the agent layer could reach before.

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# One health write per integration per process. A lookup a minute is enough
# to keep last success current; the calls in between are the same source.
_LOOKUP_GAP = 60.0
_lookup_noted: Dict[str, float] = {}
_lookup_tasks: set = set()

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
    from core.integrations.mcp.surface import VIGIL_SERVER

    for server in sorted(servers, key=len, reverse=True):
        prefix = f"{server}_"
        if flat.startswith(prefix) and len(flat) > len(prefix):
            return server, flat[len(prefix) :]

    # Vigil's own tools carry no prefix, so a name that matches no server is
    # one of them -- checked after the prefixes, so a vendor tool whose name
    # happens to look bare is still routed to the vendor.
    if VIGIL_SERVER in servers:
        return VIGIL_SERVER, flat
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


def row_error(rows: Optional[List[Any]]) -> Optional[str]:
    """The failure a tool's rows are carrying, or None when they are data.

    An ``error`` key, or a text-only reply that is an error message. Okta's
    server reports a failed call that way (``### Error executing tool``)
    rather than an ``error`` field, and that text is not a successful read.
    """
    rows = rows or []
    for row in rows:
        if isinstance(row, dict) and row.get("error"):
            error = row["error"]
            detail = str(error)
            if isinstance(error, str) and error.isidentifier():
                message = next(
                    (
                        r.get("message")
                        for r in rows
                        if isinstance(r, dict) and r.get("message")
                    ),
                    "",
                )
                detail = str(message or error)
            return detail
    texts: List[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"text"}:
            return None
        if row.get("text"):
            texts.append(str(row["text"]))
    if not texts:
        return None
    blob = " ".join(texts)
    lowered = blob.lower()
    if "error executing tool" in lowered or lowered.startswith("error"):
        return blob
    return None


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


def _error_row(rows: Any) -> bool:
    if isinstance(rows, dict):
        return bool(rows.get("error"))
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get("error") for row in rows)


def _integration_for(server: str) -> Optional[str]:
    """The one integration that owns ``server``, or None."""
    from core.integrations._base.descriptor import iter_descriptors
    from core.integrations.mcp.surface import VIGIL_SERVER

    if server == VIGIL_SERVER:
        return None
    found = [
        descriptor.id
        for descriptor in iter_descriptors()
        if server in descriptor.mcp_server_names
    ]
    if len(found) != 1:
        return None
    return found[0]


def _note_lookup(server: str, rows: Any) -> None:
    """A successful read notes that integration, at most once a minute.

    A source scheduled collection polls ignores the note. Every other
    integration records the read as a success. A bad indicator is not a health
    signal, so an error row writes nothing. Nothing here raises into the tool
    call, and nothing from the row is logged.
    """
    try:
        if _error_row(rows):
            return
        source_id = _integration_for(server)
        if source_id is None:
            return
        now = time.monotonic()
        last = _lookup_noted.get(source_id)
        if last is not None and now - last < _LOOKUP_GAP:
            return
        _lookup_noted[source_id] = now
        task = asyncio.ensure_future(_write_lookup(source_id))
        _lookup_tasks.add(task)
        task.add_done_callback(_lookup_tasks.discard)
    except Exception:  # noqa: BLE001 - the tool result stands without its note
        logger.debug("Lookup health note skipped for %s", server)


async def _write_lookup(source_id: str) -> None:
    from core.storage.source_health import record_lookup

    try:
        await asyncio.to_thread(record_lookup, source_id)
    except Exception:  # noqa: BLE001
        logger.debug("Lookup health write skipped for %s", source_id)


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

    from core.integrations.mcp.surface import VIGIL_SERVER

    if server == VIGIL_SERVER:
        # Vigil's own tools are functions in this process. Reaching them by
        # starting a second copy of Vigil and speaking down a pipe to it bought
        # isolation that was already spent: the HTTP surface is served by this
        # same app, so a Vigil that is down takes its tools with it either way.
        from core.integrations.mcp import in_process

        result = await in_process.call_tool(tool, args, timeout=timeout_s)
    else:
        from core.integrations.mcp.client import process_mcp_client

        client = process_mcp_client()
        if client is None:
            raise MCPFailure(
                UNAVAILABLE, f"{server} is configured but no client is running"
            )

        result = await client.call_tool(server, tool, args, timeout=timeout_s)
    if not isinstance(result, dict):
        rows: Any = [result]
    elif result.get("error"):
        detail = _text_of(result)
        raise MCPFailure(_failure_kind(detail), detail)
    else:
        rows = rows_from(result)
    _note_lookup(server, rows)
    return rows, True
