#!/usr/bin/env python3
# Gates the Claude Code client's connection to Vigil. The client is seven skills
# and one MCP server entry; without the entry the plugin still installs and
# still validates, and every skill then fails for want of a `vigil` server.
#
# That happened: the entry was first written as clients/claude-code/.mcp.json,
# .gitignore drops every .mcp.json, and 1.1.0 through 1.1.3 shipped without it.
# The entry now lives in plugin.json, and this checks it stays there and stays
# wired to the two values the plugin asks the operator for.

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "clients" / "claude-code" / ".claude-plugin" / "plugin.json"

SERVER = "vigil"
SURFACE = "/mcp"
REFERENCE = re.compile(r"\$\{user_config\.([A-Za-z_][A-Za-z0-9_]*)\}")
BEARER = re.compile(r"^Bearer \$\{user_config\.([A-Za-z_][A-Za-z0-9_]*)\}$")


def problems(manifest: dict[str, Any]) -> list[str]:
    found: list[str] = []
    options = manifest.get("userConfig")
    if not isinstance(options, dict):
        options = {}

    servers = manifest.get("mcpServers")
    if not isinstance(servers, dict) or SERVER not in servers:
        declared = sorted(servers) if isinstance(servers, dict) else servers
        return [
            f"plugin.json declares no `{SERVER}` MCP server inline "
            f"(mcpServers={declared!r}); the skills have nothing to call"
        ]

    server = servers[SERVER]
    if not isinstance(server, dict):
        return [f"mcpServers.{SERVER} is {type(server).__name__}, not an object"]

    if server.get("type") != "http":
        found.append(f"mcpServers.{SERVER}.type is {server.get('type')!r}, not 'http'")

    url = server.get("url")
    if not isinstance(url, str) or not url.endswith(SURFACE):
        found.append(f"mcpServers.{SERVER}.url is {url!r}; it must end in {SURFACE}")
    elif not REFERENCE.search(url):
        found.append(
            f"mcpServers.{SERVER}.url is {url!r}; the address is the operator's, "
            "so it must come from a ${user_config.*} option"
        )

    headers = server.get("headers")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    bearer = BEARER.match(authorization) if isinstance(authorization, str) else None
    if bearer is None:
        found.append(
            f"mcpServers.{SERVER}.headers.Authorization is {authorization!r}; "
            "it must be exactly 'Bearer ${user_config.<option>}'"
        )
    else:
        option = options.get(bearer.group(1))
        if isinstance(option, dict) and option.get("sensitive") is not True:
            found.append(
                f"userConfig.{bearer.group(1)} carries the credential and is not "
                f"marked sensitive (sensitive={option.get('sensitive')!r})"
            )

    for name in sorted(set(REFERENCE.findall(json.dumps(server)))):
        if name not in options:
            found.append(
                f"mcpServers.{SERVER} references ${{user_config.{name}}}, which "
                f"userConfig does not declare (declared: {sorted(options)})"
            )
    return found


def main() -> int:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"{MANIFEST.relative_to(ROOT)}: cannot be read: {error}")
        return 1
    if not isinstance(manifest, dict):
        print(f"{MANIFEST.relative_to(ROOT)}: is not a JSON object")
        return 1

    found = problems(manifest)
    for problem in found:
        print(f"{MANIFEST.relative_to(ROOT)}: {problem}")
    if found:
        return 1
    print(
        f"{MANIFEST.relative_to(ROOT)}: the `{SERVER}` MCP server is declared and wired"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
