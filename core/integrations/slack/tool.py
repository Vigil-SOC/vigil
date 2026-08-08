import asyncio
import json
import logging
import requests
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from core.config import get_integration_config

logger = logging.getLogger(__name__)
server = Server("slack")


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return get_integration_config('slack')


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(name="slack_send_alert", description="Send security alert to Slack channel",
            inputSchema={"type": "object", "properties": {
                "channel": {"type": "string"}, "message": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]}
            }, "required": ["channel", "message"]}),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    config = get_config()
    token = config.get('bot_token')
    if not token:
        return result({"error": "Slack not configured"})
    
    args = arguments or {}
    
    if name == "slack_send_alert":
        channel = args.get("channel")
        msg = args.get("message")
        if not channel or not msg:
            return result({"error": "channel and message required"})
        
        sev = args.get("severity", "medium")
        colors = {"low": "#36a64f", "medium": "#ffcc00", "high": "#ff9900", "critical": "#ff0000"}
        
        try:
            resp = requests.post("https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "channel": channel,
                    "attachments": [{
                        "color": colors.get(sev, "#808080"),
                        "title": f"Security Alert - {sev.upper()}",
                        "text": msg
                    }]
                }, timeout=30)
            data = resp.json()
            if data.get("ok"):
                return result({"success": True, "channel": channel, "ts": data.get("ts")})
            return result({"error": data.get("error", "Unknown error")})
        except Exception as e:
            return result({"error": str(e)})
    
    return result({"error": f"Unknown tool: {name}"})


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="slack", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={})
        ))


if __name__ == "__main__":
    asyncio.run(main())
