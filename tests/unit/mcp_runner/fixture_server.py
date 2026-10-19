"""A stdio MCP server for the runner tests: reports its env, sleeps on request."""

import os

import anyio
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

TOOLS = [
    types.Tool(name="env", inputSchema={"type": "object"}),
    types.Tool(name="sleep", inputSchema={"type": "object"}),
]


async def on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(_ctx, params):
    args = params.arguments or {}
    if params.name == "sleep":
        await anyio.sleep(float(args.get("seconds", 1)))
        text = "slept"
    else:
        text = os.environ.get(args["name"], "<unset>")
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("fixture", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="fixture",
                server_version="0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
