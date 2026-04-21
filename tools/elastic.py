"""Elastic Security MCP tool server.

Exposes Elasticsearch search and Kibana Security API capabilities as
MCP tools for use by Vigil's AI agents.
"""

import asyncio
import json
import logging
import os

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)
server = Server("elastic")

_elastic_service = None


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_elastic_service():
    global _elastic_service
    if _elastic_service is not None:
        return _elastic_service
    try:
        from services.elastic_service import ElasticService
        host = os.environ.get("ELASTIC_HOST")
        if not host:
            return None
        _elastic_service = ElasticService(
            elasticsearch_url=host,
            kibana_url=os.environ.get("ELASTIC_KIBANA_URL"),
            api_key=os.environ.get("ELASTIC_API_KEY"),
            username=os.environ.get("ELASTIC_USERNAME"),
            password=os.environ.get("ELASTIC_PASSWORD"),
            verify_ssl=os.environ.get("ELASTIC_VERIFY_SSL", "true").lower() == "true",
            index_pattern=os.environ.get(
                "ELASTIC_INDEX_PATTERN", ".alerts-security.alerts-default"
            ),
        )
        return _elastic_service
    except Exception:
        return None


# ------------------------------------------------------------------
# Tool listing
# ------------------------------------------------------------------

@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(
            name="elastic_search_logs",
            description="Search Elasticsearch logs with a custom query DSL body",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Elasticsearch query DSL as a JSON string",
                    },
                    "index": {
                        "type": "string",
                        "description": "Target index (default: configured alert index)",
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
            name="elastic_search_by_ioc",
            description="Search Elasticsearch for events matching an IOC (IP, hash, username, hostname)",
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
            name="elastic_get_indices",
            description="List available Elasticsearch indices",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="elastic_get_detection_alerts",
            description="Fetch recent detection alerts from Elastic Security",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 50},
                    "status": {
                        "type": "string",
                        "enum": ["open", "acknowledged", "closed"],
                        "description": "Filter by alert status",
                    },
                },
            },
        ),
    ]


# ------------------------------------------------------------------
# Tool dispatch
# ------------------------------------------------------------------

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    svc = get_elastic_service()
    if svc is None:
        return result({
            "error": "Elastic service not configured. Set ELASTIC_HOST in .env."
        })

    if name == "elastic_search_logs":
        return await _search_logs(svc, arguments or {})
    elif name == "elastic_search_by_ioc":
        return await _search_by_ioc(svc, arguments or {})
    elif name == "elastic_get_indices":
        return await _get_indices(svc)
    elif name == "elastic_get_detection_alerts":
        return await _get_detection_alerts(svc, arguments or {})
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
        index=args.get("index"),
        size=min(args.get("max_results", 100), 500),
    )
    if data is None:
        return result({"error": "Search failed"})

    hits = data.get("hits", {})
    return result({
        "total": hits.get("total", {}).get("value", 0),
        "results": [
            {"_id": h["_id"], **h.get("_source", {})}
            for h in hits.get("hits", [])
        ],
    })


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
    return result({
        "ioc_type": ioc_type,
        "ioc_value": ioc_value,
        "total": hits.get("total", {}).get("value", 0),
        "results": [
            {"_id": h["_id"], **h.get("_source", {})}
            for h in hits.get("hits", [])
        ],
    })


async def _get_indices(svc):
    indices = await svc.get_indices()
    if indices is None:
        return result({"error": "Failed to list indices"})
    return result({"indices": indices, "count": len(indices)})


async def _get_detection_alerts(svc, args: dict):
    query: dict = {"match_all": {}}
    status = args.get("status")
    if status:
        query = {"term": {"kibana.alert.workflow_status": status}}

    data = await svc.fetch_detection_alerts(
        query=query,
        size=min(args.get("max_results", 50), 200),
    )
    if data is None:
        return result({"error": "Failed to fetch detection alerts"})

    hits = data.get("hits", {})
    return result({
        "total": hits.get("total", {}).get("value", 0),
        "alerts": [
            {
                "_id": h["_id"],
                "rule": h.get("_source", {}).get("kibana.alert.rule.name", ""),
                "severity": h.get("_source", {}).get("kibana.alert.severity", ""),
                "timestamp": h.get("_source", {}).get("@timestamp", ""),
            }
            for h in hits.get("hits", [])
        ],
    })


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="elastic",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
