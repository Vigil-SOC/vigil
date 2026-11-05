"""OpenCTI MCP tool server.

Read-only lookups over the OpenCTI GraphQL API: observables and their
indicators, one indicator's detail, named entities (actors, malware, reports,
...) and an entity's relationships, or the objects a report names.
"""

import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/opencti/tool.py`` with a narrowed env,
# so the repo root is not on sys.path; add it so the ``core.*`` imports resolve.
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
from core.integrations.opencti.client import (
    ENTITY_QUERIES,
    OpenCTIError,
    OpenCTIService,
)
from core.integrations.opencti.descriptor import OPENCTI, OPENCTI_REQUIRED

logger = logging.getLogger(__name__)
_service = None


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_service():
    """The service, or the names of the fields still missing."""
    global _service
    if _service is not None:
        return _service, ()
    config = resolve(OPENCTI)
    absent = missing(config, *OPENCTI_REQUIRED)
    if absent:
        return None, absent
    # resolve() returns every declared field; an unset verify_ssl means verify.
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    _service = OpenCTIService(
        url=config["url"],
        api_token=config["api_token"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )
    return _service, ()


def _limit_schema(default):
    return {"type": "integer", "default": default, "minimum": 1, "maximum": 50}


async def handle_list_tools():
    return [
        types.Tool(
            name="opencti_search_observable",
            description=(
                "Look up an observable (IP, domain, URL, file hash) in OpenCTI. Returns "
                "matching observables with score, labels and linked indicators; "
                "exact_match marks results equal to the value asked for."
            ),
            inputSchema={
                "type": "object",
                "properties": {"value": {"type": "string"}, "limit": _limit_schema(10)},
                "required": ["value"],
            },
        ),
        types.Tool(
            name="opencti_get_indicator",
            description="Get one OpenCTI indicator: STIX pattern, validity window, score, labels, kill-chain phases.",
            inputSchema={
                "type": "object",
                "properties": {"indicator_id": {"type": "string"}},
                "required": ["indicator_id"],
            },
        ),
        types.Tool(
            name="opencti_search_entities",
            description=(
                "Search OpenCTI by name or alias for threat actors, intrusion sets, malware, "
                "attack patterns, tools, campaigns or reports. To read what a report names, "
                "pass its id to opencti_get_related."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "entity_type": {"type": "string", "enum": sorted(ENTITY_QUERIES)},
                    "limit": _limit_schema(10),
                },
                "required": ["query", "entity_type"],
            },
        ),
        types.Tool(
            name="opencti_get_related",
            description=(
                "List the relationships going out of an OpenCTI entity (uses, targets, "
                "attributed-to, ...), capped. entity_id is the id another OpenCTI tool "
                "returned (a STIX id matches nothing); each target carries its own id. "
                "For a report (or grouping, case) it also lists objects: the attack "
                "patterns, intrusion sets, indicators, ... the report names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "limit": _limit_schema(20),
                },
                "required": ["entity_id"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    service, absent = get_service()
    if service is None:
        return result({"error": f"OpenCTI not configured: missing {', '.join(absent)}"})
    args = arguments or {}
    try:
        if name == "opencti_search_observable":
            if not args.get("value"):
                return result({"error": "value required"})
            return result(
                await service.search_observable(args["value"], args.get("limit", 10))
            )
        if name == "opencti_get_indicator":
            if not args.get("indicator_id"):
                return result({"error": "indicator_id required"})
            return result(await service.get_indicator(args["indicator_id"]))
        if name == "opencti_search_entities":
            if not args.get("query") or not args.get("entity_type"):
                return result({"error": "query and entity_type required"})
            return result(
                await service.search_entities(
                    args["query"], args["entity_type"], args.get("limit", 10)
                )
            )
        if name == "opencti_get_related":
            if not args.get("entity_id"):
                return result({"error": "entity_id required"})
            return result(
                await service.get_related(args["entity_id"], args.get("limit", 20))
            )
    except OpenCTIError as exc:
        return result({"error": str(exc)})
    return result({"error": f"Unknown tool: {name}"})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "opencti",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="opencti",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
