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

from core.integrations._base.aad_token import TokenError, fetch_client_credentials_token
from core.integrations._base.config import resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.microsoft_defender.descriptor import MICROSOFT_DEFENDER

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_token():
    """Access token, or None when credentials are not configured.

    Raises ``TokenError`` when they are configured but the exchange fails.
    """
    config = resolve(MICROSOFT_DEFENDER)
    tenant = config.get("tenant_id")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not all([tenant, client_id, client_secret]):
        return None
    return fetch_client_credentials_token(
        "Microsoft Defender",
        tenant,
        client_id,
        client_secret,
        "https://api.securitycenter.microsoft.com/.default",
    )


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


async def handle_call_tool(name: str, arguments: dict | None):
    try:
        token = get_token()
    except TokenError as exc:
        return result({"error": str(exc)})
    if not token:
        return result({"error": "Microsoft Defender not configured"})

    args = arguments or {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://api.securitycenter.microsoft.com/api"

    try:
        if name == "mde_get_alerts":
            resp = httpx.get(
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
            resp = httpx.get(f"{base}/machines/{mid}", headers=headers, timeout=30)
            resp.raise_for_status()
            return result({"machine": resp.json()})

        elif name == "mde_isolate":
            mid = args.get("machine_id")
            comment = args.get("comment")
            if not mid or not comment:
                return result({"error": "machine_id and comment required"})
            resp = httpx.post(
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


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "microsoft-defender",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


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
