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


# Global singleton
