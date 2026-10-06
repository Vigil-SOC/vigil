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

from core.integrations._base.config import missing, resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.carbon_black.descriptor import CARBON_BLACK

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def handle_list_tools():
    return [
        types.Tool(
            name="cb_get_alerts",
            description="Get Carbon Black alerts",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 10}},
                "required": [],
            },
        ),
        types.Tool(
            name="cb_search_device",
            description="Search device by IP/hostname",
            inputSchema={
                "type": "object",
                "properties": {
                    "ip": {"type": "string"},
                    "hostname": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="cb_quarantine",
            description="Quarantine device",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["device_id", "reason"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = resolve(CARBON_BLACK)
    url = config.get("url")
    org_key = config.get("org_key")
    if missing(config, "url", "api_id", "api_key", "org_key"):
        return result({"error": "Carbon Black not configured"})

    args = arguments or {}
    # CBC authenticates with the API Secret Key and API ID joined by a slash.
    # This read a single `api_token` the Settings form never collects, so the
    # header was always "None".
    token = f"{config.get('api_key')}/{config.get('api_id')}"
    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}

    try:
        if name == "cb_get_alerts":
            resp = httpx.post(
                f"{url}/appservices/v6/orgs/{org_key}/alerts/_search",
                headers=headers,
                json={"rows": args.get("limit", 10)},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            alerts = [
                {
                    "id": a.get("id"),
                    "severity": a.get("severity"),
                    "device_name": a.get("device_name"),
                    "reason": a.get("reason"),
                }
                for a in data.get("results", [])
            ]
            return result({"count": len(alerts), "alerts": alerts})

        elif name == "cb_search_device":
            query = []
            if args.get("ip"):
                query.append(f"device_external_ip:{args['ip']}")
            if args.get("hostname"):
                query.append(f"device_name:{args['hostname']}")
            if not query:
                return result({"error": "ip or hostname required"})

            resp = httpx.post(
                f"{url}/appservices/v6/orgs/{org_key}/devices/_search",
                headers=headers,
                json={"query": " OR ".join(query)},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return result(
                {
                    "count": len(data.get("results", [])),
                    "devices": data.get("results", [])[:5],
                }
            )

        elif name == "cb_quarantine":
            did = args.get("device_id")
            if not did:
                return result({"error": "device_id required"})
            resp = httpx.post(
                f"{url}/appservices/v6/orgs/{org_key}/device_actions",
                headers=headers,
                json={"action_type": "QUARANTINE", "device_id": [did]},
                timeout=30,
            )
            resp.raise_for_status()
            return result({"success": True, "device_id": did, "action": "quarantined"})

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "carbon-black",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="carbon-black",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
