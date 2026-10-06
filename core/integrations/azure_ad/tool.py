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
from core.integrations.azure_ad.descriptor import AZURE_AD

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_token():
    """Access token, or None when credentials are not configured.

    Raises ``TokenError`` when they are configured but the exchange fails.
    """
    config = resolve(AZURE_AD)
    tenant = config.get("tenant_id")
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not all([tenant, client_id, client_secret]):
        return None
    return fetch_client_credentials_token(
        "Azure AD",
        tenant,
        client_id,
        client_secret,
        "https://graph.microsoft.com/.default",
    )


async def handle_list_tools():
    return [
        types.Tool(
            name="aad_get_user",
            description="Get Azure AD user",
            inputSchema={
                "type": "object",
                "properties": {"user": {"type": "string"}},
                "required": ["user"],
            },
        ),
        types.Tool(
            name="aad_disable_user",
            description="Disable Azure AD user",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id", "reason"],
            },
        ),
        types.Tool(
            name="aad_get_sign_ins",
            description="Get user sign-in logs",
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    try:
        token = get_token()
    except TokenError as exc:
        return result({"error": str(exc)})
    if not token:
        return result({"error": "Azure AD not configured"})

    args = arguments or {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        if name == "aad_get_user":
            user = args.get("user")
            if not user:
                return result({"error": "user required"})
            resp = httpx.get(
                f"https://graph.microsoft.com/v1.0/users/{user}",
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 404:
                return result({"user": user, "found": False})
            resp.raise_for_status()
            data = resp.json()
            return result(
                {
                    "found": True,
                    "id": data.get("id"),
                    "displayName": data.get("displayName"),
                    "accountEnabled": data.get("accountEnabled"),
                    "mail": data.get("mail"),
                }
            )

        elif name == "aad_disable_user":
            uid = args.get("user_id")
            if not uid:
                return result({"error": "user_id required"})
            resp = httpx.patch(
                f"https://graph.microsoft.com/v1.0/users/{uid}",
                headers=headers,
                json={"accountEnabled": False},
                timeout=30,
            )
            resp.raise_for_status()
            return result({"success": True, "user_id": uid, "action": "disabled"})

        elif name == "aad_get_sign_ins":
            # `or 20`, not a .get default: a tool call may carry "limit": null,
            # and httpx renders a None param as "$top=" where requests dropped it.
            params = {"$top": args.get("limit") or 20}
            if user := args.get("user"):
                params["$filter"] = f"userPrincipalName eq '{user}'"
            resp = httpx.get(
                "https://graph.microsoft.com/v1.0/auditLogs/signIns",
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            logs = resp.json().get("value", [])
            return result({"count": len(logs), "sign_ins": logs})

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "azure-ad",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="azure-ad",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
