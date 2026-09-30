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
from core.integrations._base.tls import tls_verify
from core.integrations.misp.descriptor import MISP

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return resolve(MISP)


async def handle_list_tools():
    return [
        types.Tool(
            name="misp_search_ioc",
            description="Search IOC in MISP",
            inputSchema={
                "type": "object",
                "properties": {"value": {"type": "string"}, "type": {"type": "string"}},
                "required": ["value"],
            },
        ),
        types.Tool(
            name="misp_get_events",
            description="Get recent MISP events",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 10}},
                "required": [],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = get_config()
    api_key = config.get("api_key")
    url = config.get("url")
    # resolve() always returns every declared field, so a .get(k, True) default
    # would never fire — verify_ssl is present-but-None when unset.
    verify = True if config.get("verify_ssl") is None else config.get("verify_ssl")
    ca_cert_path = config.get("ca_cert_path")
    if missing(config, "url", "api_key"):
        return result({"error": "MISP not configured"})

    args = arguments or {}
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    try:
        if name == "misp_search_ioc":
            value = args.get("value")
            if not value:
                return result({"error": "value required"})
            resp = httpx.post(
                f"{url}/attributes/restSearch",
                headers=headers,
                json={"value": value},
                timeout=30,
                verify=tls_verify(verify, ca_cert_path),
            )
            resp.raise_for_status()
            data = resp.json()
            attrs = data.get("response", {}).get("Attribute", [])
            return result(
                {
                    "value": value,
                    "found": len(attrs) > 0,
                    "count": len(attrs),
                    "attributes": attrs[:10],
                }
            )

        elif name == "misp_get_events":
            limit = args.get("limit", 10)
            resp = httpx.post(
                f"{url}/events/restSearch",
                headers=headers,
                json={"limit": limit, "returnFormat": "json"},
                timeout=30,
                verify=tls_verify(verify, ca_cert_path),
            )
            resp.raise_for_status()
            data = resp.json()
            events = data.get("response", [])
            return result(
                {
                    "count": len(events),
                    "events": [
                        {
                            "id": e.get("Event", {}).get("id"),
                            "info": e.get("Event", {}).get("info"),
                            "date": e.get("Event", {}).get("date"),
                        }
                        for e in events[:limit]
                    ],
                }
            )

        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    try:
        content = await handle_call_tool(params.name, params.arguments)
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))],
            is_error=True,
        )
    return types.CallToolResult(content=content)


server = Server(
    "misp",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="misp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
