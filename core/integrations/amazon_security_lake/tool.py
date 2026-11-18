"""Amazon Security Lake MCP tool server.

One tool, ``security_lake_query``: Athena SQL over the OCSF sources of one region
per call. Discovery runs at start and its findings are the tool's description.
"""

import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/amazon_security_lake/tool.py`` with a
# narrowed env: put the repo root on sys.path so the ``core.*`` imports resolve.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.amazon_security_lake import lake
from core.integrations.amazon_security_lake.descriptor import AMAZON_SECURITY_LAKE

logger = logging.getLogger(__name__)

TOOL = "security_lake_query"
_START_BUDGET_S = 12.0
_REGION_BUDGET_S = 6.0

_discovery: Optional[asyncio.Task] = None
_sessions: Dict[Tuple[Any, Any], Any] = {}


def result(data):
    return [
        types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))
    ]


def _config() -> Dict[str, Any]:
    config = resolve(AMAZON_SECURITY_LAKE)
    return {
        **config,
        "workgroup": config.get("workgroup") or "primary",
        "allowed": lake.parse_regions(config.get("allowed_regions")),
    }


def _session(config: Dict[str, Any]):
    key = (config.get("role_arn"), config.get("external_id"))
    if key not in _sessions:
        _sessions[key] = lake.build_session(config)
    return _sessions[key]


def _discover_all() -> lake.Discovery:
    try:
        config = _config()
    except Exception as exc:  # noqa: BLE001 - reported in the tool description
        return lake.Discovery("aws", {}, error=lake.describe_exc(exc))
    return lake.discover(lambda: _session(config), _START_BUDGET_S)


def _start_discovery() -> asyncio.Task:
    global _discovery
    if _discovery is None:
        _discovery = asyncio.ensure_future(asyncio.to_thread(_discover_all))
    return _discovery


def _describe(found: lake.Discovery, allowed: Tuple[str, ...]) -> str:
    lines = [
        "Run one read-only Athena SQL query over Amazon Security Lake (OCSF), in one "
        "AWS region per call; cover several regions with one call each. Use a source's "
        "short name as a table (e.g. SELECT ... FROM cloud_trail_mgmt): the server "
        "limits it to [start, end) on its date partition and event time. Do not name "
        "Security Lake databases or tables directly. Only a single SELECT or WITH "
        "statement runs. Select cloud.region and cloud.account.uid to keep each row's "
        "provenance. Every answer reports region, query_id, the range, bytes_scanned "
        "and row_count; rows come back under ocsf_events. A region not allowed to "
        "return rows answers with a pointer only (row count and bytes, no rows). A "
        "query still running when the call ends keeps running: collect it with the "
        "same region and its query_id.",
    ]
    if found.error:
        lines.append(f"Discovery could not read AWS at start ({found.error}).")
    for region in found.named(lake.READABLE):
        sources = found.regions[region].sources
        mode = "returns rows" if region in allowed else "pointer only, no rows"
        listed = ", ".join(
            f"{s.name} (OCSF {s.ocsf_version})" for s in sources.values()
        )
        lines.append(f"{region} [{mode}]: {listed}.")
    if refused := found.named(lake.REFUSED):
        lines.append(
            f"Refused by Glue or Lake Formation (a visibility gap): {', '.join(refused)}."
        )
    if unreachable := found.named(lake.UNREACHABLE):
        lines.append(f"Did not answer discovery: {', '.join(unreachable)}.")
    return "\n".join(lines)


async def handle_list_tools():
    found = await asyncio.shield(_start_discovery())
    try:
        allowed = _config()["allowed"]
    except Exception:  # noqa: BLE001 - no config reads as no region returning rows
        allowed = ()
    return [
        types.Tool(
            name=TOOL,
            description=lake.scrub(_describe(found, allowed)),
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "The one AWS region to query, e.g. us-east-1",
                    },
                    "sql": {
                        "type": "string",
                        "description": "Athena (Trino) SQL naming sources by short name",
                    },
                    "start": {
                        "type": "string",
                        "description": "Range start, ISO 8601 (inclusive)",
                    },
                    "end": {
                        "type": "string",
                        "description": "Range end, ISO 8601 (exclusive)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 100,
                        "maximum": lake.MAX_RESULTS,
                    },
                    "query_id": {
                        "type": "string",
                        "description": "Collect an earlier query in this region instead of running sql",
                    },
                },
                "required": ["region"],
            },
        )
    ]


async def _region_state(config, region: str) -> lake.RegionState:
    found = await asyncio.shield(_start_discovery())
    state = found.regions.get(region)
    if state is None or state.state != lake.READABLE:
        # Look again: access may have been granted since the server started.
        state = await asyncio.wait_for(
            asyncio.to_thread(lake.discover_region, _session(config), region),
            _REGION_BUDGET_S,
        )
        found.regions[region] = state
    return state


async def _query(args: Dict[str, Any]) -> Dict[str, Any]:
    # The deadline counts from here, so waiting on discovery spends it too.
    started = time.monotonic()
    config = _config()
    region = str(args.get("region") or "").strip().lower()
    if not region:
        raise lake.LakeError("region is required: one AWS region per call.")
    found = await asyncio.shield(_start_discovery())
    if found.regions and region not in found.regions:
        raise lake.LakeError(
            f"{region} is not a region of the {found.partition} partition."
        )
    try:
        max_results = int(args.get("max_results") or 100)
    except (TypeError, ValueError):
        raise lake.LakeError("max_results must be an integer.") from None
    ctx = lake.QueryContext(
        region=region,
        allowed=region in config["allowed"],
        workgroup=config["workgroup"],
        max_results=max(1, min(max_results, lake.MAX_RESULTS)),
    )
    session = _session(config)
    query_id = str(args.get("query_id") or "").strip()
    state = found.regions.get(region) or lake.RegionState(lake.ABSENT)
    if query_id:
        return await asyncio.to_thread(
            lake.collect, session, ctx, query_id, state.sources, started
        )
    ctx.start = lake.parse_time(args.get("start"), "start")
    ctx.end = lake.parse_time(args.get("end"), "end")
    if ctx.start >= ctx.end:
        raise lake.LakeError("start must be before end.")
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise lake.LakeError("sql is required unless collecting by query_id.")
    state = await _region_state(config, region)
    if state.state == lake.REFUSED:
        raise lake.LakeError(
            f"Region {region} is refused: Glue or Lake Formation denied read access "
            f"({state.detail}). It is a visibility gap until access is granted."
        )
    if state.state == lake.UNREACHABLE:
        raise lake.LakeError(f"Region {region} did not answer ({state.detail}).")
    if state.state == lake.ABSENT:
        raise lake.LakeError(f"No Security Lake source is readable in {region}.")
    return await asyncio.to_thread(
        lake.run_query, session, ctx, sql, state.sources, started
    )


async def handle_call_tool(name: str, arguments: Optional[dict]):
    if name != TOOL:
        return result({"error": f"Unknown tool: {name}"})
    args = arguments or {}
    try:
        return result(await _query(args))
    except lake.LakeError as exc:
        message = str(exc)
    except asyncio.TimeoutError:
        message = f"Region {args.get('region')} did not answer discovery in time."
    except Exception as exc:  # noqa: BLE001 - never pass AWS text through raw
        message = f"Security Lake query in {args.get('region')} failed ({lake.describe_exc(exc)})."
    return result(
        {
            "error": lake.scrub(message),
            "region": args.get("region"),
            **({"query_id": args["query_id"]} if args.get("query_id") else {}),
        }
    )


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "amazon-security-lake",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    _start_discovery()
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="amazon-security-lake",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
