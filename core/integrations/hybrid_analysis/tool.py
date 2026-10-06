import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/<vendor>/tool.py`` with a narrowed env,
# so the repo root is not on sys.path and PYTHONPATH is not forwarded. Add it
# here so the ``core.*`` imports below resolve; otherwise they fail at spawn.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import json
import logging

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.hybrid_analysis.descriptor import HYBRID_ANALYSIS

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def handle_list_tools():
    return [
        types.Tool(
            name="ha_search_hash",
            description="Search Hybrid Analysis by file hash",
            inputSchema={
                "type": "object",
                "properties": {"hash": {"type": "string"}},
                "required": ["hash"],
            },
        ),
        types.Tool(
            name="ha_get_report",
            description="Get analysis report",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = resolve(HYBRID_ANALYSIS)
    api_key = config.get("api_key")
    if not api_key:
        return result({"error": "Hybrid Analysis not configured"})

    args = arguments or {}
    headers = {"api-key": api_key, "User-Agent": "Falcon Sandbox"}

    try:
        if name == "ha_search_hash":
            h = args.get("hash")
            if not h:
                return result({"error": "hash required"})
            resp = httpx.post(
                "https://www.hybrid-analysis.com/api/v2/search/hash",
                headers=headers,
                data={"hash": h},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return result({"hash": h, "found": len(data) > 0, "results": data[:5]})

        elif name == "ha_get_report":
            jid = args.get("job_id")
            if not jid:
                return result({"error": "job_id required"})
            resp = httpx.get(
                f"https://www.hybrid-analysis.com/api/v2/report/{jid}/summary",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            return result({"job_id": jid, "report": resp.json()})

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "hybrid-analysis",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="hybrid-analysis",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
