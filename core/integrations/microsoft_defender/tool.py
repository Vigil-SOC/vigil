import asyncio
import json
import logging

import mcp.server.stdio
import mcp.types as types
import requests
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.config import get_integration_config

logger = logging.getLogger(__name__)
server = Server("microsoft-defender")


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_token():
    config = get_integration_config("microsoft-defender")
    tenant = config.get("tenant_id")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not all([tenant, client_id, client_secret]):
        return None
    try:
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://api.securitycenter.microsoft.com/.default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception:
        return None


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(
            name="mde_get_alerts",
            description="Get Microsoft Defender alerts",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
                "required": [],
            },
        ),
        types.Tool(
            name="mde_get_machine",
            description="Get machine info",
            inputSchema={
                "type": "object",
                "properties": {"machine_id": {"type": "string"}},
                "required": ["machine_id"],
            },
        ),
        types.Tool(
            name="mde_isolate",
            description="Isolate machine",
            inputSchema={
                "type": "object",
                "properties": {
                    "machine_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["machine_id", "comment"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    token = get_token()
    if not token:
        return result({"error": "Microsoft Defender not configured"})

    args = arguments or {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://api.securitycenter.microsoft.com/api"

    try:
        if name == "mde_get_alerts":
            resp = requests.get(
                f"{base}/alerts",
                headers=headers,
                params={"$top": args.get("limit", 20)},
                timeout=30,
            )
            resp.raise_for_status()
            alerts = [
                {
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "severity": a.get("severity"),
                    "status": a.get("status"),
                }
                for a in resp.json().get("value", [])
            ]
            return result({"count": len(alerts), "alerts": alerts})

        elif name == "mde_get_machine":
            mid = args.get("machine_id")
            if not mid:
                return result({"error": "machine_id required"})
            resp = requests.get(f"{base}/machines/{mid}", headers=headers, timeout=30)
            resp.raise_for_status()
            return result({"machine": resp.json()})

        elif name == "mde_isolate":
            mid = args.get("machine_id")
            comment = args.get("comment")
            if not mid or not comment:
                return result({"error": "machine_id and comment required"})
            resp = requests.post(
                f"{base}/machines/{mid}/isolate",
                headers=headers,
                json={"Comment": comment, "IsolationType": "Full"},
                timeout=30,
            )
            resp.raise_for_status()
            return result({"success": True, "machine_id": mid, "action": "isolated"})

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="microsoft-defender",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
