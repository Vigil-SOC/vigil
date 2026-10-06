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
from urllib.parse import urlparse

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
            name="analyze_url",
            description="Analyze URL for security indicators",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
        types.Tool(
            name="extract_iocs",
            description="Extract IOCs from URL",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    args = arguments or {}

    if name == "analyze_url":
        url = args.get("url")
        if not url:
            return result({"error": "url required"})
        try:
            parsed = urlparse(url)
            analysis = {
                "url": url,
                "scheme": parsed.scheme,
                "domain": parsed.netloc,
                "path": parsed.path,
                "suspicious_indicators": [],
            }
            if parsed.scheme != "https":
                analysis["suspicious_indicators"].append("Non-HTTPS")
            if any(c in parsed.netloc for c in ["@", ":", "%"]):
                analysis["suspicious_indicators"].append(
                    "Suspicious characters in domain"
                )
            if len(parsed.path) > 100:
                analysis["suspicious_indicators"].append("Unusually long path")
            return result(analysis)
        except Exception as e:
            return result({"error": str(e)})

    elif name == "extract_iocs":
        url = args.get("url")
        if not url:
            return result({"error": "url required"})
        try:
            parsed = urlparse(url)
            return result(
                {
                    "url": url,
                    "iocs": {
                        "domain": (
                            parsed.netloc.split(":")[0] if parsed.netloc else None
                        ),
                        "ip": None,
                        "port": parsed.port,
                        "path": parsed.path,
                    },
                }
            )
        except Exception as e:
            return result({"error": str(e)})

    return result({"error": f"Unknown tool: {name}"})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "url-analysis",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="url-analysis",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
