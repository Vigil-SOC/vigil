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

    Used by agents to dynamically discover what tools
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

    def set_active(self, server_name: str, active: bool) -> None:
        """Mark one server active or inactive. No-op when it is not registered."""
        info = self._servers.get(server_name)
        if info is None:
            return
        info["active"] = active

    def retain_only(self, active_names: List[str]) -> None:
        """Mark exactly ``active_names`` active; deactivate every other server."""
        wanted = set(active_names)
        for name, info in self._servers.items():
            info["active"] = name in wanted

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

                # Prefix the description with the server so the model sees a
                # tool's provenance — but leave it empty when the tool has none,
                # so a downstream "drop tools with no description" guard still
                # fires (a fabricated "[server] " would read as truthy).
                raw_desc = (tool.get("description") or "").strip()
                description = f"[{server_name}] {raw_desc}" if raw_desc else ""

                all_tools.append(
                    {
                        "name": tool_name,
                        "description": description,
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


# Whether this deployment dials every configured MCP server at startup. Off by
# default under DEV_MODE; an explicit ``mcp_auto_connect_on_startup`` wins either
# way. ``populate_from_cache`` uses it to decide whether live connection state
# gates the warm-start cache. services/api/main.py makes the same call for its
# own startup path; core/ cannot import services/, so the rule lives here.
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


def safe_tool_names(registry: Optional[MCPRegistry]) -> List[str]:
    """Tool names from ``registry``, or [] when it cannot be reached.

    Shared by the agent and workflow AI generators, whose prompt-building is
    best-effort: an unavailable registry means "recommend no tools", never an
    error. Takes the registry rather than reaching for a global, so callers
    keep whatever instance they were injected with.
    """
    try:
        return list((registry or MCPRegistry()).get_tool_names() or [])
    except Exception as e:
        logger.debug(f"MCP registry unavailable: {e}")
        return []


def register_connected(registry: MCPRegistry, mcp_client, server_name: str) -> bool:
    """Register one server from the client's ``tools_cache``.

    Called where a connect succeeds so the registry tracks enable intent, not
    the next reader's liveness check. Best effort: a failure logs at debug and
    returns False, so an enable endpoint can keep its own result.
    """
    try:
        tools = (getattr(mcp_client, "tools_cache", None) or {}).get(server_name)
        if not isinstance(tools, list) or not tools:
            return False
        registry.register_server(
            server_name,
            _server_config(mcp_client, server_name),
            [_normalised(t) for t in tools],
        )
        return True
    except Exception as exc:  # noqa: BLE001 — callers must not 500 on a registry write
        logger.debug("Could not register MCP server %s: %s", server_name, exc)
        return False


def deactivate(registry: MCPRegistry, server_name: str) -> None:
    """Stop offering a server's tools. Best effort; unknown names are a no-op."""
    try:
        registry.set_active(server_name, False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not deactivate MCP server %s: %s", server_name, exc)


def refresh_from_client(registry: MCPRegistry) -> int:
    """Add servers that connected after startup from the client's ``tools_cache``.

    Unlike ``populate_from_cache`` (a boot warm-start that prefers the on-disk
    tool cache), this reads the running client's ``tools_cache`` and current
    connection status so a server connected *after* startup — e.g. a dormant
    retry came online, or ``call_tool`` created a session — becomes usable on
    the next turn without a restart. Cheap and idempotent; call it wherever a
    turn assembles its tool list.

    Does not deactivate. Removal follows disable via ``deactivate``, not
    liveness: ``is_connected`` goes False on any failed call and stays there
    until the next one reconnects, so it is not "unreachable".
    """
    from core.integrations.mcp.client import process_mcp_client

    mcp_client = process_mcp_client()
    if mcp_client is None:
        return 0
    tools_dict = getattr(mcp_client, "tools_cache", None) or {}
    try:
        connected = mcp_client.get_connection_status() or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read MCP connection status: %s", exc)
        connected = {}

    added = 0
    for name, tools in tools_dict.items():
        if connected and not connected.get(name, False):
            continue
        registry.register_server(
            name, _server_config(mcp_client, name), [_normalised(t) for t in tools]
        )
        added += 1
    return added


def live_mcp_tools(registry: MCPRegistry) -> List[Dict]:
    """The MCP integrations currently offered, Claude-API-shaped, for one turn.

    Refreshes the registry from the running client (add-path for servers
    connected outside the enable endpoint), then returns its tools
    (server-prefixed names). Returns ``[]`` — never raises — when the client or
    registry is unavailable, so a caller can fall back to built-in tools. This
    is the one call a request path needs to surface live integrations.
    """
    try:
        refresh_from_client(registry)
        return registry.get_all_tools() or []
    except Exception as exc:  # noqa: BLE001 — callers degrade to built-in tools
        logger.debug("Live MCP tool surface unavailable: %s", exc)
        return []
