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
from core.integrations.alienvault_otx.descriptor import ALIENVAULT_OTX

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return resolve(ALIENVAULT_OTX)


async def handle_list_tools():
    return [
        types.Tool(
            name="otx_check_ip",
            description="Check IP in AlienVault OTX",
            inputSchema={
                "type": "object",
                "properties": {"ip": {"type": "string"}},
                "required": ["ip"],
            },
        ),
        types.Tool(
            name="otx_check_domain",
            description="Check domain in OTX",
            inputSchema={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
                "required": ["domain"],
            },
        ),
        types.Tool(
            name="otx_check_hash",
            description="Check file hash in OTX",
            inputSchema={
                "type": "object",
                "properties": {"hash": {"type": "string"}},
                "required": ["hash"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = get_config()
    api_key = config.get("api_key")
    if not api_key:
        return result({"error": "AlienVault OTX not configured"})

    args = arguments or {}
    headers = {"X-OTX-API-KEY": api_key}
    base = "https://otx.alienvault.com/api/v1"

    try:
        if name == "otx_check_ip":
            ip = args.get("ip")
            if not ip:
                return result({"error": "ip required"})
            resp = httpx.get(
                f"{base}/indicators/IPv4/{ip}/general", headers=headers, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return result(
                {
                    "ip": ip,
                    "pulse_count": data.get("pulse_info", {}).get("count", 0),
                    "reputation": data.get("reputation", 0),
                    "country": data.get("country_name"),
                }
            )

        elif name == "otx_check_domain":
            domain = args.get("domain")
            if not domain:
                return result({"error": "domain required"})
            resp = httpx.get(
                f"{base}/indicators/domain/{domain}/general",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return result(
                {
                    "domain": domain,
                    "pulse_count": data.get("pulse_info", {}).get("count", 0),
                    "alexa": data.get("alexa"),
                    "whois": data.get("whois"),
                }
            )

        elif name == "otx_check_hash":
            h = args.get("hash")
            if not h:
                return result({"error": "hash required"})
            resp = httpx.get(
                f"{base}/indicators/file/{h}/general", headers=headers, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return result(
                {
                    "hash": h,
                    "pulse_count": data.get("pulse_info", {}).get("count", 0),
                    "type": data.get("type_title"),
                }
            )

        return result({"error": f"Unknown tool: {name}"})
    # HTTPStatusError, not HTTPError: the parent also catches transport errors,
    # which belong to the handler below.
    except httpx.HTTPStatusError as e:
        return result(
            {
                "error": "API error",
                "status": e.response.status_code if hasattr(e, "response") else None,
            }
        )
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "alienvault-otx",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="alienvault-otx",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
