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

from core.integrations._base.tool_result import run_tool

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def handle_list_tools():
    return [
        types.Tool(
            name="geolocate_ip",
            description="Get geolocation for IP address",
            inputSchema={
                "type": "object",
                "properties": {"ip": {"type": "string"}},
                "required": ["ip"],
            },
        ),
        types.Tool(
            name="geolocate_batch",
            description="Geolocate multiple IPs",
            inputSchema={
                "type": "object",
                "properties": {"ips": {"type": "array", "items": {"type": "string"}}},
                "required": ["ips"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    args = arguments or {}

    def lookup_ip(ip):
        try:
            resp = httpx.get(f"http://ip-api.com/json/{ip}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return {
                        "ip": ip,
                        "country": data.get("country"),
                        "region": data.get("regionName"),
                        "city": data.get("city"),
                        "isp": data.get("isp"),
                        "org": data.get("org"),
                        "lat": data.get("lat"),
                        "lon": data.get("lon"),
                    }
            return {"ip": ip, "error": "Lookup failed"}
        except Exception as e:
            return {"ip": ip, "error": str(e)}

    if name == "geolocate_ip":
        ip = args.get("ip")
        if not ip:
            return result({"error": "ip required"})
        return result(lookup_ip(ip))

    elif name == "geolocate_batch":
        ips = args.get("ips", [])
        if not ips:
            return result({"error": "ips required"})
        results = [lookup_ip(ip) for ip in ips[:10]]
        return result({"count": len(results), "results": results})

    return result({"error": f"Unknown tool: {name}"})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "ip-geolocation",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="ip-geolocation",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
