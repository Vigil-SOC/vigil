"""Regenerate ``chat-catalogue.json`` — the tool catalogue the context tests
assemble against.

The fixture is the catalogue a chat turn actually declares, produced by the
repo's own declaration path (``core.llm.chat_layers._declare``) rather than
hand-written sizes: the 34 built-in tools, Vigil's own MCP server's tools
(``core.integrations.mcp.in_process.list_tools``) and the tools of every MCP
server this repo ships itself — the ``python3 core/integrations/*/tool.py``
entries in mcp-config.json — each server-prefixed the way the registry
stores them, shaped the way the agent layer receives them
({id, description, parameters}).

Vendor MCP servers run from outside packages (virustotal, shodan, …) are
discovered live and their schemas are not in this repo, so they are not in
the fixture either; a stack with several of them connected declares more
than this. The fixture is the floor a default install declares, not the
ceiling.

Run from the repo root with the project venv:

    DEV_MODE=true VIGIL_DISABLE_DOTENV=1 python \
        services/agent/tests/fixtures/generate-chat-catalogue.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))


def _in_repo_servers() -> list[tuple[str, str]]:
    """(server name, module) for each MCP server this repo ships itself."""
    config = json.loads((REPO / "mcp-config.json").read_text())
    servers = []
    for name, server in config["mcpServers"].items():
        if name.startswith("_comment"):
            continue
        args = server.get("args") or []
        path = next((a for a in args if a.endswith("tool.py")), None)
        if path is None:
            continue
        module = path.removesuffix(".py").replace("/", ".")
        servers.append((name, module))
    return servers


def main() -> None:
    import importlib

    from core.integrations.mcp.in_process import list_tools as vigil_tools
    from core.llm.chat_layers import _declare

    mcp = [
        {
            "name": f"vigil_{tool['name']}",
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        }
        for tool in vigil_tools()
    ]
    for server_name, module_name in _in_repo_servers():
        module = importlib.import_module(module_name)
        tools = asyncio.run(module.handle_list_tools())
        mcp += [
            {
                "name": f"{server_name}_{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.model_dump(by_alias=True).get("inputSchema")
                or {"type": "object"},
            }
            for tool in tools
        ]
        print(f"{server_name}: {len(tools)} tools")

    declared = _declare(None, mcp)
    fixture = [
        {"id": t["id"], "description": t["description"], "parameters": t["parameters"]}
        for t in declared
    ]
    out = Path(__file__).with_name("chat-catalogue.json")
    out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
    blob = json.dumps(fixture, separators=(",", ":"), ensure_ascii=False)
    print(f"{len(fixture)} tools, {len(blob)} chars serialised -> {out}")


if __name__ == "__main__":
    main()
