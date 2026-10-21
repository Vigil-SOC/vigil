import sys
from pathlib import Path

# Spawned as ``python3 core/integrations/<vendor>/tool.py`` with a narrowed env,
# so the repo root is not on sys.path and PYTHONPATH is not forwarded. Add it
# here so the ``core.*`` imports below resolve; otherwise they fail at spawn.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio
import ipaddress
import json
from urllib.parse import quote, quote_plus

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from core.integrations._base.config import resolve
from core.integrations._base.tool_result import run_tool
from core.integrations.ip_geolocation.descriptor import IP_GEOLOCATION

# ip-api.com's free host has no TLS. Pro is the HTTPS endpoint.
_PRO_URL = "https://pro.ip-api.com/json/{ip}"
_NOT_CONFIGURED = "IP Geolocation not configured"


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return resolve(IP_GEOLOCATION)


def _refused(ip: str):
    """Local answer for input that must not leave the install. None when public."""
    if not isinstance(ip, str):
        return {"ip": ip, "error": "invalid IP address"}
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return {"ip": ip, "error": "invalid IP address"}
    # Multicast, reserved, and site-local can still be flagged global.
    # Shared ranges such as 100.64/10 are neither private nor global.
    if (
        parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_unspecified
        or getattr(parsed, "is_site_local", False)
        or not parsed.is_global
    ):
        return {"ip": ip, "error": "non-public address"}
    return None


def _redact(text: str, api_key: str) -> str:
    # httpx puts the query string in transport errors, percent-encoded.
    if not api_key:
        return text
    # quote_plus turns a space into '+', which is how httpx writes the query.
    for secret in {api_key, quote(api_key, safe=""), quote_plus(api_key)}:
        if secret:
            text = text.replace(secret, "redacted")
    return text


def lookup_ip(ip: str, api_key: str):
    refused = _refused(ip)
    if refused is not None:
        return refused
    parsed = ipaddress.ip_address(ip)
    try:
        resp = httpx.get(
            _PRO_URL.format(ip=parsed),
            params={"key": api_key},
            timeout=10,
        )
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
        return {"ip": ip, "error": _redact(str(e), api_key)}


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
    api_key = get_config().get("api_key")
    if not api_key:
        return result({"error": _NOT_CONFIGURED})

    args = arguments or {}

    if name == "geolocate_ip":
        ip = args.get("ip")
        if not ip:
            return result({"error": "ip required"})
        return result(lookup_ip(ip, api_key))

    elif name == "geolocate_batch":
        ips = args.get("ips", [])
        if not ips:
            return result({"error": "ips required"})
        results = [lookup_ip(ip, api_key) for ip in ips[:10]]
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
