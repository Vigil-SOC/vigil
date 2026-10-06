"""OpenSearch MCP tool server.

Exposes OpenSearch search and Security Analytics findings as MCP tools
for use by Vigil's AI agents.
"""

import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/opensearch/tool.py`` with a narrowed
# env, so the repo root is not on sys.path and PYTHONPATH is not forwarded.
# Add it here so the ``core.*`` imports below resolve; otherwise they fail
# and every query silently reports "not configured".
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import json
import logging

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import missing, resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.opensearch.client import LOG_INDICES, OpenSearchService
from core.integrations.opensearch.descriptor import OPENSEARCH

logger = logging.getLogger(__name__)
_opensearch_service = None


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_opensearch_service():
    global _opensearch_service
    if _opensearch_service is not None:
        return _opensearch_service
    try:
        config = resolve(OPENSEARCH)
        if missing(config, "opensearch_url"):
            return None
        # resolve() always returns every declared field, so a .get(k, True)
        # default would never fire — verify_ssl is present-but-None when unset.
        verify = True if config.get("verify_ssl") is None else config.get("verify_ssl")
        _opensearch_service = OpenSearchService(
            opensearch_url=config["opensearch_url"],
            dashboards_url=config.get("dashboards_url"),
            username=config.get("username"),
            password=config.get("password"),
            verify_ssl=verify,
            index_pattern=config.get("index_pattern") or ".opensearch-sap-*-findings-*",
            ca_cert_path=config.get("ca_cert_path"),
        )
        return _opensearch_service
    except Exception:
        return None


# ------------------------------------------------------------------
# Tool listing
# ------------------------------------------------------------------


async def handle_list_tools():
    return [
        types.Tool(
            name="opensearch_search_logs",
            description="Search OpenSearch logs with a custom query DSL body",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "OpenSearch query DSL as a JSON string",
                    },
                    "index": {
                        "type": "string",
                        "description": "Target index (default: all non-system indices)",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Relative time range, e.g. '24h', '7d'",
                        "default": "24h",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="opensearch_search_by_ioc",
            description="Search OpenSearch for events matching an IOC (IP, hash, username, hostname)",
            inputSchema={
                "type": "object",
                "properties": {
                    "ioc_type": {
                        "type": "string",
                        "enum": ["ip", "hash", "username", "hostname"],
                    },
                    "ioc_value": {"type": "string"},
                    "index": {"type": "string"},
                    "hours": {"type": "integer", "default": 24},
                },
                "required": ["ioc_type", "ioc_value"],
            },
        ),
        types.Tool(
            name="opensearch_get_indices",
            description="List available OpenSearch indices",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="opensearch_get_findings",
            description="Fetch recent Security Analytics findings from OpenSearch",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 50},
                    "detector_name": {
                        "type": "string",
                        "description": "Filter by Security Analytics detector name",
                    },
                },
            },
        ),
    ]


# ------------------------------------------------------------------
# Tool dispatch
# ------------------------------------------------------------------


async def handle_call_tool(name: str, arguments: dict | None):
    svc = get_opensearch_service()
    if svc is None:
        return result({"error": "OpenSearch service not configured"})

    if name == "opensearch_search_logs":
        return await _search_logs(svc, arguments or {})
    elif name == "opensearch_search_by_ioc":
        return await _search_by_ioc(svc, arguments or {})
    elif name == "opensearch_get_indices":
        return await _get_indices(svc)
    elif name == "opensearch_get_findings":
        return await _get_findings(svc, arguments or {})
    else:
        return result({"error": f"Unknown tool: {name}"})


async def _search_logs(svc, args: dict):
    query_str = args.get("query", '{"match_all": {}}')
    try:
        query = json.loads(query_str) if isinstance(query_str, str) else query_str
    except json.JSONDecodeError:
        return result({"error": "Invalid JSON in query parameter"})

    time_range = args.get("time_range", "24h")
    # Wrap with time filter
    wrapped = {
        "bool": {
            "must": [query],
            "filter": [{"range": {"@timestamp": {"gte": f"now-{time_range}"}}}],
        }
    }

    data = await svc.search(
        query=wrapped,
        index=args.get("index") or LOG_INDICES,
        size=min(args.get("max_results", 100), 500),
    )
    if data is None:
        return result({"error": "Search failed"})

    hits = data.get("hits", {})
    return result(
        {
            "total": hits.get("total", {}).get("value", 0),
            "results": [
                {"_id": h["_id"], **h.get("_source", {})} for h in hits.get("hits", [])
            ],
        }
    )


async def _search_by_ioc(svc, args: dict):
    ioc_type = args["ioc_type"]
    ioc_value = args["ioc_value"]
    index = args.get("index")
    hours = args.get("hours", 24)

    dispatch = {
        "ip": svc.search_by_ip,
        "hash": svc.search_by_hash,
        "username": svc.search_by_username,
        "hostname": svc.search_by_hostname,
    }
    fn = dispatch.get(ioc_type)
    if fn is None:
        return result({"error": f"Unknown ioc_type: {ioc_type}"})

    data = await fn(ioc_value, index=index, hours=hours)
    if data is None:
        return result({"error": "Search failed"})

    hits = data.get("hits", {})
    return result(
        {
            "ioc_type": ioc_type,
            "ioc_value": ioc_value,
            "total": hits.get("total", {}).get("value", 0),
            "results": [
                {"_id": h["_id"], **h.get("_source", {})} for h in hits.get("hits", [])
            ],
        }
    )


async def _get_indices(svc):
    indices = await svc.get_indices()
    if indices is None:
        return result({"error": "Failed to list indices"})
    return result({"indices": indices, "count": len(indices)})


async def _get_findings(svc, args: dict):
    query: dict = {"match_all": {}}
    detector_name = args.get("detector_name")
    if detector_name:
        query = {"term": {"monitor_name": detector_name}}

    data = await svc.search(
        query=query,
        size=min(args.get("max_results", 50), 200),
        sort=[{"timestamp": {"order": "desc"}}],
    )
    if data is None:
        return result({"error": "Failed to fetch findings"})

    hits = data.get("hits", {})
    return result(
        {
            "total": hits.get("total", {}).get("value", 0),
            "findings": [
                {
                    "_id": h["_id"],
                    "finding_id": h.get("_source", {}).get("id", ""),
                    "detector_name": h.get("_source", {}).get("monitor_name", ""),
                    "rules": [
                        q.get("name", q.get("id", ""))
                        for q in h.get("_source", {}).get("queries", [])
                    ],
                    "timestamp": h.get("_source", {}).get("timestamp", ""),
                    "index": h.get("_index", ""),
                }
                for h in hits.get("hits", [])
            ],
        }
    )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "opensearch",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="opensearch",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
