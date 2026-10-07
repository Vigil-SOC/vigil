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
from core.integrations.microsoft_teams.descriptor import MICROSOFT_TEAMS

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


async def handle_list_tools():
    return [
        types.Tool(
            name="teams_send_alert",
            description="Send alert to Teams channel via webhook",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                },
                "required": ["title", "message"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = resolve(MICROSOFT_TEAMS)
    webhook = config.get("webhook_url")
    if not webhook:
        return result({"error": "Microsoft Teams not configured"})

    args = arguments or {}

    if name == "teams_send_alert":
        title = args.get("title")
        msg = args.get("message")
        if not title or not msg:
            return result({"error": "title and message required"})

        sev = args.get("severity", "medium")
        colors = {
            "low": "00FF00",
            "medium": "FFFF00",
            "high": "FFA500",
            "critical": "FF0000",
        }

        try:
            resp = httpx.post(
                webhook,
                json={
                    "@type": "MessageCard",
                    "@context": "http://schema.org/extensions",
                    "themeColor": colors.get(sev, "808080"),
                    "summary": title,
                    "sections": [
                        {
                            "activityTitle": f"Security Alert: {title}",
                            "facts": [
                                {"name": "Severity", "value": sev.upper()},
                                {"name": "Details", "value": msg},
                            ],
                        }
                    ],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return result({"success": True, "title": title})
            return result({"error": f"HTTP {resp.status_code}"})
        except Exception as e:
            return result({"error": str(e)})

    return result({"error": f"Unknown tool: {name}"})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "microsoft-teams",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="microsoft-teams",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
