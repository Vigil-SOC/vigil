# Chat's on-demand door to the connected MCP integrations (#1959): search their
# tools, and resolve a call to one of them. Reads the live registry per call, so
# a server connected mid-conversation is found on the next search.

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from core.agents.mcp_tools import MCPFailure

if TYPE_CHECKING:
    from core.integrations.mcp.registry import MCPRegistry

MAX_MATCHES = 8

# Words that would match nearly every description.
_STOPWORDS = frozenset(
    "a an and any are by for from get in is of on or the to with".split()
)

_ATTACK_ID = re.compile(r"t[0-9]{4}(\.[0-9]{3})?")


def _reachable(registry: "MCPRegistry") -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    from core.integrations.mcp.registry import live_mcp_tools
    from core.llm.chat_layers import integration_tools

    return integration_tools(live_mcp_tools(registry)), registry.tool_servers()


def _terms(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
    # A trailing plural "s" is dropped so "rules" finds "rule".
    return [
        w[:-1] if len(w) > 3 and w.endswith("s") else w
        for w in words
        if w not in _STOPWORDS
    ]


def find_integration_tools(
    registry: "MCPRegistry", query: str, server: Optional[str] = None
) -> List[Dict[str, Any]]:
    tools, servers = _reachable(registry)
    if server:
        tools = [t for t in tools if servers.get(t["name"]) == server]
    terms = set(_terms(query or ""))
    # An ATT&CK id names no tool, but says what kind of tool is wanted.
    if any(_ATTACK_ID.fullmatch(term) for term in terms):
        terms |= {"mitre", "technique"}
    # Whole words of the server, name, description and input schema (whose enums
    # say which sources a tool reads). A name hit counts double, and a rare word
    # more than a common one.
    names = {
        t["name"]: set(_terms(f"{servers.get(t['name'], '')} {t['name']}"))
        for t in tools
    }
    words = {
        t["name"]: names[t["name"]]
        | set(_terms(t.get("description") or ""))
        | set(_terms(json.dumps(t.get("input_schema") or {})))
        for t in tools
    }
    rarity = {
        term: math.log((1 + len(tools)) / (1 + sum(term in w for w in words.values())))
        + 1
        for term in terms
    }
    scored = [
        (
            sum(
                rarity[term]
                * (2 * (term in names[t["name"]]) + (term in words[t["name"]]))
                for term in terms
            ),
            t,
        )
        for t in tools
    ]
    ranked = sorted(
        (pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1]["name"])
    )
    return [
        {
            "name": tool["name"],
            "server": servers.get(tool["name"]),
            "description": tool.get("description") or "",
            "input_schema": tool.get("input_schema") or {"type": "object"},
        }
        for _, tool in ranked[:MAX_MATCHES]
    ]


def resolve_integration_call(
    registry: "MCPRegistry", args: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """The tool and arguments a ``call_integration_tool`` call stands for.

    Refused, as an ungranted tool is, when the name is not one chat may reach:
    not connected, a built-in, or excluded by ``_is_destructive_mcp``.
    """
    name = str(args.get("name") or "")
    arguments = args.get("arguments") or {}
    tools, _ = _reachable(registry)
    if name not in {t["name"] for t in tools}:
        raise MCPFailure("refused", f"{name or '(no name)'} is not granted to chat")
    if not isinstance(arguments, dict):
        raise MCPFailure("invalid_args", "arguments must be an object")
    return name, arguments
