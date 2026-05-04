"""MCP server exposing VStrike (CloudCurrent) topology lookups as tools.

Read-only surface. Writes (quarantine, isolate) are intentionally not
exposed in this server — they require the approval workflow.

Tools:
  - vstrike_get_asset_topology
  - vstrike_list_adjacent_assets
  - vstrike_get_blast_radius
  - vstrike_get_segment_findings
  - vstrike_node_search
  - vstrike_node_drift_get
  - vstrike_storyline_list
  - vstrike_storyline_events_get
  - vstrike_legend_run_list
  - vstrike_legend_run_results_get
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
        types.Tool(
            name="vstrike_node_search",
            description=(
                "Omni-search across nodes in the active VStrike network. "
                "Returns the nodes that caused the match."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "network_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="vstrike_node_drift_get",
            description=(
                "Returns the list of end-node state changes in order for the "
                "supplied node and what source identified each change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "network_id": {"type": "string"},
                },
                "required": ["node_id"],
            },
        ),
        types.Tool(
            name="vstrike_storyline_list",
            description=(
                "List the storylines available for the network."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "network_id": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="vstrike_storyline_events_get",
            description=(
                "List the events in the storylines along with their properties."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "storyline_id": {"type": "string"},
                    "network_id": {"type": "string"},
                },
                "required": ["storyline_id"],
            },
        ),
        types.Tool(
            name="vstrike_legend_run_list",
            description=(
                "List the legend runs available for the network."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "network_id": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="vstrike_legend_run_results_get",
            description=(
                "Returns the results for the legend provided."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "legend_run_id": {"type": "string"},
                    "network_id": {"type": "string"},
                },
                "required": ["legend_run_id"],
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

    if name == "vstrike_node_search":
        query = args.get("query")
        if not query:
            return _result({"error": "query required"})
        network_id = args.get("network_id")
        limit = int(args.get("limit", 50))
        result = service.node_search(query, network_id=network_id, limit=limit)
        if result is None:
            return _result({"error": f"Node search failed for query {query}"})
        return _result({"query": query, "network_id": network_id, "results": result})

    if name == "vstrike_node_drift_get":
        node_id = args.get("node_id")
        if not node_id:
            return _result({"error": "node_id required"})
        network_id = args.get("network_id")
        result = service.node_drift_get(node_id, network_id=network_id)
        if result is None:
            return _result({"error": f"Node drift failed for {node_id}"})
        return _result({"node_id": node_id, "network_id": network_id, "drift": result})

    if name == "vstrike_storyline_list":
        network_id = args.get("network_id")
        result = service.storyline_list(network_id=network_id)
        if result is None:
            return _result({"error": "Storyline list failed"})
        return _result({"network_id": network_id, "storylines": result})

    if name == "vstrike_storyline_events_get":
        storyline_id = args.get("storyline_id")
        if not storyline_id:
            return _result({"error": "storyline_id required"})
        network_id = args.get("network_id")
        result = service.storyline_events_get(storyline_id, network_id=network_id)
        if result is None:
            return _result({"error": f"Storyline events failed for {storyline_id}"})
        return _result({"storyline_id": storyline_id, "network_id": network_id, "events": result})

    if name == "vstrike_legend_run_list":
        network_id = args.get("network_id")
        result = service.legend_run_list(network_id=network_id)
        if result is None:
            return _result({"error": "Legend run list failed"})
        return _result({"network_id": network_id, "legend_runs": result})

    if name == "vstrike_legend_run_results_get":
        legend_run_id = args.get("legend_run_id")
        if not legend_run_id:
            return _result({"error": "legend_run_id required"})
        network_id = args.get("network_id")
        result = service.legend_run_results_get(legend_run_id, network_id=network_id)
        if result is None:
            return _result({"error": f"Legend run results failed for {legend_run_id}"})
        return _result({"legend_run_id": legend_run_id, "network_id": network_id, "results": result})

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
