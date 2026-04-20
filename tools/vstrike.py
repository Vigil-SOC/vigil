"""MCP server exposing VStrike (CloudCurrent) topology lookups as tools.

Read-only surface. Writes (quarantine, isolate) are intentionally not
exposed in this server — they require the approval workflow.

Tools:
  - vstrike_get_asset_topology
  - vstrike_list_adjacent_assets
  - vstrike_get_blast_radius
  - vstrike_get_segment_findings
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Ensure the repo root is on sys.path so `services.*` imports work when this
# module is launched as a stand-alone MCP server via stdio.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)
server = Server("vstrike")


def _result(data) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _get_service():
    try:
        from services.vstrike_service import get_vstrike_service

        return get_vstrike_service()
    except Exception as e:
        logger.error("Failed to load VStrike service: %s", e)
        return None


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(
            name="vstrike_get_asset_topology",
            description=(
                "Return full topology info for a VStrike asset: segment, "
                "site, criticality, neighbors."
            ),
            inputSchema={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
            },
        ),
        types.Tool(
            name="vstrike_list_adjacent_assets",
            description=(
                "List one-hop neighbors of an asset. Each neighbor may "
                "include a MITRE ATT&CK technique if the edge represents "
                "an observed or inferred attack-path step."
            ),
            inputSchema={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
            },
        ),
        types.Tool(
            name="vstrike_get_blast_radius",
            description=(
                "Return blast-radius info for an asset (count of reachable "
                "assets plus a sample)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"asset_id": {"type": "string"}},
                "required": ["asset_id"],
            },
        ),
        types.Tool(
            name="vstrike_get_segment_findings",
            description=(
                "List VStrike-enriched findings for a given network segment."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "segment": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["segment"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    args = arguments or {}
    service = _get_service()
    if service is None:
        return _result(
            {
                "error": "VStrike not configured",
                "message": (
                    "Set VSTRIKE_BASE_URL + VSTRIKE_API_KEY or configure the "
                    "integration in Settings > Integrations."
                ),
            }
        )

    if name == "vstrike_get_asset_topology":
        asset_id = args.get("asset_id")
        if not asset_id:
            return _result({"error": "asset_id required"})
        topology = service.get_asset_topology(asset_id)
        if topology is None:
            return _result({"error": f"No topology returned for {asset_id}"})
        return _result({"asset_id": asset_id, "topology": topology})

    if name == "vstrike_list_adjacent_assets":
        asset_id = args.get("asset_id")
        if not asset_id:
            return _result({"error": "asset_id required"})
        adjacent = service.list_adjacent(asset_id)
        if adjacent is None:
            return _result({"error": f"No adjacency returned for {asset_id}"})
        return _result({"asset_id": asset_id, "adjacent": adjacent})

    if name == "vstrike_get_blast_radius":
        asset_id = args.get("asset_id")
        if not asset_id:
            return _result({"error": "asset_id required"})
        blast = service.get_blast_radius(asset_id)
        if blast is None:
            return _result({"error": f"No blast radius returned for {asset_id}"})
        return _result({"asset_id": asset_id, "blast_radius": blast})

    if name == "vstrike_get_segment_findings":
        segment = args.get("segment")
        if not segment:
            return _result({"error": "segment required"})
        limit = int(args.get("limit", 100))
        findings = service.find_findings_by_segment(segment, limit=limit)
        if findings is None:
            return _result({"error": f"Failed to fetch findings for segment {segment}"})
        return _result(
            {"segment": segment, "count": len(findings), "findings": findings}
        )

    return _result({"error": f"Unknown tool: {name}"})


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="vstrike",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
