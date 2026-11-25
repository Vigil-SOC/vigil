"""Elastic Security MCP tool server.

Exposes Elasticsearch search and Kibana Security API capabilities as
MCP tools for use by Vigil's AI agents.
"""

import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/elastic/tool.py`` with a narrowed env, so
# the repo root is not on sys.path and PYTHONPATH is not forwarded. Add it here so
# the ``core.*`` imports below resolve; otherwise they fail and every query
# silently reports "not configured".
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
from core.integrations._base.search_params import timestamp_filter, validate_time_range
from core.integrations._base.tool_result import run_tool
from core.integrations.elastic.client import ElasticService
from core.integrations.elastic.descriptor import ELASTIC, ELASTIC_REQUIRED

logger = logging.getLogger(__name__)
_elastic_service = None
_config_failed = False


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_elastic_service():
    global _elastic_service, _config_failed
    _config_failed = False
    if _elastic_service is not None:
        return _elastic_service
    try:
        config = resolve(ELASTIC)
        if missing(config, *ELASTIC_REQUIRED):
            return None
        # resolve() always returns every declared field, so a .get(k, True)
        # default would never fire — verify_ssl is present-but-None when unset.
        verify = True if config.get("verify_ssl") is None else config.get("verify_ssl")
        _elastic_service = ElasticService(
            elasticsearch_url=config["elasticsearch_url"],
            kibana_url=config.get("kibana_url"),
            api_key=config.get("api_key"),
            username=config.get("username"),
            password=config.get("password"),
            verify_ssl=verify,
            index_pattern=config.get("index_pattern")
            or ".alerts-security.alerts-default",
            ca_cert_path=config.get("ca_cert_path"),
        )
        return _elastic_service
    except Exception as exc:
        _config_failed = True
        logger.error("Elastic config/client setup failed: %s", exc)
        return None


# ------------------------------------------------------------------
# Tool listing
# ------------------------------------------------------------------


async def handle_list_tools():
    return [
        types.Tool(
            name="elastic_search_logs",
            description=(
                "Search Elasticsearch with a query clause, limited to the last "
                "time_range by an @timestamp filter. Pass fields to return only "
                "those fields of each hit, and aggs to aggregate (with max_results "
                "0 for aggregations alone)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Elasticsearch query clause as JSON, e.g. "
                        '{"term": {"user.name": "alice"}}. It is wrapped in a bool '
                        "query with the @timestamp filter, so pass the clause, not a "
                        "whole request body.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fields to return for each hit (sent as _source "
                        "includes; wildcards allowed). Default: the whole document; [] for "
                        "none.",
                    },
                    "aggs": {
                        "type": "object",
                        "description": "Elasticsearch aggregations object, sent as the "
                        "request's aggs; the result carries them under aggregations.",
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
                        "description": "Hits to return, 0-500; 0 for aggregations only",
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


async def handle_call_tool(name: str, arguments: dict | None):
    svc = get_elastic_service()
    if svc is None:
        if _config_failed:
            return result(
                {
                    "error": "Elastic is configured but its connection settings "
                    "could not be loaded; check the server log"
                }
            )
        return result({"error": "Elastic service not configured"})

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


def _json_arg(value):
    """A JSON-string argument parsed, or an object/list passed through as is."""
    return json.loads(value) if isinstance(value, str) else value


async def _search_logs(svc, args: dict):
    try:
        query = _json_arg(args.get("query", {"match_all": {}}))
        fields = args.get("fields")
        # A bare field name is not JSON; anything else that is a string should be.
        if isinstance(fields, str) and fields.lstrip()[:1] in ("[", "{"):
            fields = json.loads(fields)
        aggs = _json_arg(args.get("aggs"))
    except json.JSONDecodeError:
        return result({"error": "Invalid JSON in query, fields or aggs"})

    max_results = args.get("max_results")
    # Models send whole request bodies; no query clause type is named "query",
    # so an object keyed "query" is a body. Explicit arguments win over it.
    if isinstance(query, dict) and "query" in query:
        body = query
        query = body["query"]
        if fields is None:
            fields = body.get("_source")
        if aggs is None:
            aggs = body.get("aggs", body.get("aggregations"))
        if max_results is None:
            max_results = body.get("size")
    if isinstance(fields, dict):  # _source as {"includes": [...]}
        fields = fields.get("includes")
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    if fields is True or not isinstance(fields, (list, bool)):
        fields = None  # whole document; False (or []) returns no fields
    elif fields == []:
        fields = False
    if aggs is not None and not isinstance(aggs, dict):
        return result({"error": "aggs must be a JSON object"})
    if query is None or query == {}:
        query = {"match_all": {}}

    try:
        size = max(0, min(int(100 if max_results is None else max_results), 500))
    except (TypeError, ValueError):
        return result({"error": "max_results must be an integer"})

    time_range = validate_time_range(args.get("time_range", "24h"))
    wrapped = {
        "bool": {
            "must": [query],
            "filter": [
                timestamp_filter(time_range, args.get("time_gte"), args.get("time_lte"))
            ],
        }
    }

    data = await svc.search(
        query=wrapped,
        index=args.get("index"),
        size=size,
        source=fields,
        aggs=aggs,
    )
    if data is None:
        return result({"error": "Search failed"})

    hits = data.get("hits", {})
    out = {
        "total": hits.get("total", {}).get("value", 0),
        "results": [
            {"_id": h["_id"], **h.get("_source", {})} for h in hits.get("hits", [])
        ],
    }
    if "aggregations" in data:
        out["aggregations"] = data["aggregations"]
    return result(out)


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
    return result(
        {
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
    "elastic",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


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
