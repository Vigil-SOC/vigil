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
from core.integrations.slack.descriptor import SLACK

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return resolve(SLACK)


async def handle_list_tools():
    return [
        types.Tool(
            name="slack_send_alert",
            description="Send security alert to Slack channel",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "message": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                },
                "required": ["channel", "message"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    config = get_config()
    token = config.get("bot_token")
    if not token:
        return result({"error": "Slack not configured"})

    args = arguments or {}

    if name == "slack_send_alert":
        channel = args.get("channel")
        msg = args.get("message")
        if not channel or not msg:
            return result({"error": "channel and message required"})

        sev = args.get("severity", "medium")
        colors = {
            "low": "#36a64f",
            "medium": "#ffcc00",
            "high": "#ff9900",
            "critical": "#ff0000",
        }

        try:
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channel": channel,
                    "attachments": [
                        {
                            "color": colors.get(sev, "#808080"),
                            "title": f"Security Alert - {sev.upper()}",
                            "text": msg,
                        }
                    ],
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("ok"):
                return result(
                    {"success": True, "channel": channel, "ts": data.get("ts")}
                )
            return result({"error": data.get("error", "Unknown error")})
        except Exception as e:
            return result({"error": str(e)})

    return result({"error": f"Unknown tool: {name}"})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    return await run_tool(handle_call_tool, params)


server = Server(
    "slack",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="slack",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
