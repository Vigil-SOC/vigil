"""
MCP Registry - Central registry for active MCP servers and their tools.

Provides dynamic tool discovery so Claude can automatically use
whatever MCP servers are currently active, without hardcoding.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPRegistry:
    """
    Central registry that tracks active MCP servers and their available tools.

    Used by ClaudeService and agents to dynamically discover what tools
    are available at runtime, enabling automatic enrichment from active
    MCP integrations (like security-detections, threat intel, etc.)
    """

    def __init__(self):
        self._servers: Dict[str, Dict[str, Any]] = {}
        self._tools_cache: Dict[str, List[Dict]] = {}
        self._last_refresh: Optional[datetime] = None

    def register_server(
        self, name: str, config: Dict[str, Any], tools: Optional[List[Dict]] = None
    ):
        """
        Register an MCP server and its tools.

        Args:
            name: Server name (e.g., 'security-detections', 'deeptempo-findings')
            config: Server config (command, args, env, etc.)
            tools: List of tool definitions (name, description, input_schema)
        """
        self._servers[name] = {
            "name": name,
            "config": config,
            "registered_at": datetime.now().isoformat(),
            "active": True,
        }
        if tools:
            self._tools_cache[name] = tools
        logger.info(f"Registered MCP server: {name} ({len(tools or [])} tools)")

    def get_active_servers(self) -> List[str]:
        """Get names of all active servers."""
        return [
            name for name, info in self._servers.items() if info.get("active", False)
        ]

    def get_all_tools(self) -> List[Dict]:
        """
        Get all tools from all active servers, formatted for Claude API.

        Returns:
            List of tool definitions with server-prefixed names.
        """
        all_tools = []
        seen = set()

        for server_name in self.get_active_servers():
            for tool in self._tools_cache.get(server_name, []):
                # Prefix tool name with server name (matching ClaudeService convention)
                tool_name = f"{server_name}_{tool['name']}"
                if tool_name in seen:
                    continue
                seen.add(tool_name)

                all_tools.append(
                    {
                        "name": tool_name,
                        "description": f"[{server_name}] {tool.get('description', '')}",
                        "input_schema": tool.get(
                            "input_schema",
                            tool.get(
                                "inputSchema",
                                {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                },
                            ),
                        ),
                    }
                )

        return all_tools

    def get_tool_names(self) -> List[str]:
        """Get all tool names (server-prefixed) from active servers."""
        return [t["name"] for t in self.get_all_tools()]

    def get_agent_sdk_configs(self) -> List[Dict]:
        """
        Get MCP server configurations formatted for Agent SDK's
        ClaudeAgentOptions.mcp_servers parameter.

        Returns:
            List of MCP server config dicts with name, command, args, env.
        """
        configs = []
        for name in self.get_active_servers():
            server_info = self._servers.get(name, {})
            config = server_info.get("config", {})
            if config.get("command"):
                configs.append(
                    {
                        "name": name,
                        "command": config["command"],
                        "args": config.get("args", []),
                        "env": config.get("env", {}),
                    }
                )
        return configs

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the registry state."""
        return {
            "servers": len(self._servers),
            "active_servers": len(self.get_active_servers()),
            "total_tools": sum(len(t) for t in self._tools_cache.values()),
            "last_refresh": (
                self._last_refresh.isoformat() if self._last_refresh else None
            ),
            "server_details": {
                name: {
                    "active": info.get("active", False),
                    "tools_count": len(self._tools_cache.get(name, [])),
                    "registered_at": info.get("registered_at"),
                }
                for name, info in self._servers.items()
            },
        }


# Where the live MCP tool set comes from. This used to be a side effect of
# constructing a ClaudeService: the tool loader populated the registry on its way
# past, so two AI generators depended on somebody having built an LLM client
# first. Called explicitly at startup instead (#632).
CACHE_FILE = ("data", "mcp_tools_cache.json")


def _cached_tools() -> Dict[str, List[Dict[str, Any]]]:
    import json

    from core.config import REPO_ROOT

    path = REPO_ROOT.joinpath(*CACHE_FILE)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 — a warm-start artifact, not state
            logger.warning("Could not read the MCP tools cache: %s", exc)
    return {}


def _server_config(mcp_client, name: str) -> Dict[str, Any]:
    service = getattr(mcp_client, "mcp_service", None)
    server = getattr(service, "servers", {}).get(name) if service else None
    if server is None:
        return {}
    return {"command": server.command, "args": server.args, "env": server.env}


def _normalised(tool: Dict[str, Any]) -> Dict[str, Any]:
    schema = tool.get("inputSchema", {})
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    elif not isinstance(schema, dict):
        schema = dict(schema) if schema else {}
    return {
        "name": tool.get("name", "unknown"),
        "description": tool.get("description", ""),
        "inputSchema": schema,
    }


# Whether this deployment dials every configured MCP server at startup. Defined here
# because populate_from_cache reads it and services/api/main.py enforces it. Off by
# default under DEV_MODE; an explicit setting wins either way.
def eager_connect_enabled() -> bool:
    from core.config import get_settings

    settings = get_settings()
    if settings.mcp_auto_connect_on_startup is not None:
        return bool(settings.mcp_auto_connect_on_startup)
    return not settings.dev_mode


# The disk cache is a warm-start artifact: a server can appear there and have
# failed to connect this boot. Registering it anyway lets a model claim a
# capability it cannot exercise (#129), so live connection state gates it -- but only
# where this boot actually dialled. With eager connect off nothing is connected until
# a call arrives and call_tool reconnects, so the same check drops every server and
# leaves every capability they answer unbound for the whole boot.
def populate_from_cache(registry: MCPRegistry) -> int:
    from core.integrations.mcp.client import process_mcp_client

    mcp_client = process_mcp_client()
    tools_dict = _cached_tools()
    if not tools_dict and mcp_client is not None:
        tools_dict = getattr(mcp_client, "tools_cache", None) or {}
    if not tools_dict:
        logger.info("No MCP tools to register: the cache is empty")
        return 0

    connected: Dict[str, bool] = {}
    if eager_connect_enabled() and mcp_client is not None:
        try:
            connected = mcp_client.get_connection_status() or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read MCP connection status: %s", exc)

    registered = 0
    for name, tools in tools_dict.items():
        if connected and not connected.get(name, False):
            logger.debug("Skipping %s: cached but not connected this boot", name)
            continue
        registry.register_server(
            name, _server_config(mcp_client, name), [_normalised(t) for t in tools]
        )
        registered += 1

    logger.info("MCP registry populated from %d server(s)", registered)
    return registered
