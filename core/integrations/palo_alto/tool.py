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
from core.integrations.palo_alto.descriptor import PALO_ALTO

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def handle_list_tools():
    return [
        types.Tool(
            name="pan_block_ip",
            description="Block IP on Palo Alto firewall",
            inputSchema={
                "type": "object",
                "properties": {"ip": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["ip", "reason"],
            },
        ),
        types.Tool(
            name="pan_get_threats",
            description="Get threat logs",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
                "required": [],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = resolve(PALO_ALTO)
    api_key = config.get("api_key")
    if missing(config, "hostname", "api_key"):
        return result({"error": "Palo Alto not configured"})

    # The Settings form collects a hostname; PAN-OS is reached over HTTPS.
    host = str(config.get("hostname")).rstrip("/")
    url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    # resolve() always returns every declared field, so a .get(k, True) default
    # would never fire — verify_ssl is present-but-None when unset.
    verify = True if config.get("verify_ssl") is None else config.get("verify_ssl")
    ca_cert_path = config.get("ca_cert_path")

    args = arguments or {}

    try:
        if name == "pan_block_ip":
            ip = args.get("ip")
            if not ip:
                return result({"error": "ip required"})
            # Add to EDL or address group
            resp = httpx.get(
                f"{url}/api/",
                params={
                    "type": "config",
                    "action": "set",
                    "key": api_key,
                    "xpath": f"/config/devices/entry/vsys/entry[@name='vsys1']/address/entry[@name='blocked-{ip}']",
                    "element": f"<ip-netmask>{ip}/32</ip-netmask><description>Blocked: {args.get('reason', 'security')}</description>",
                },
                verify=tls_verify(verify, ca_cert_path),
                timeout=30,
            )
            # httpx doesn't follow redirects, so a 3xx here means the configured
            # hostname is wrong. Carry the status or that is undiagnosable.
            return result(
                {
                    "success": resp.status_code == 200,
                    "status_code": resp.status_code,
                    "ip": ip,
                    "action": "blocked",
                }
            )

        elif name == "pan_get_threats":
            resp = httpx.get(
                f"{url}/api/",
                params={
                    # `or 20`: see the aad_get_sign_ins note — a null limit would
                    # reach the wire as "nlogs=" under httpx.
                    "type": "log",
                    "log-type": "threat",
                    "key": api_key,
                    "nlogs": args.get("limit") or 20,
                },
                verify=tls_verify(verify, ca_cert_path),
                timeout=30,
            )
            # Parse XML response (simplified)
            return result(
                {"success": True, "message": "Check Palo Alto console for threat logs"}
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
    "palo-alto",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="palo-alto",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
