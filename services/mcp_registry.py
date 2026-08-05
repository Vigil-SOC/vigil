import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class MCPRegistry:

    def __init__(self):
        self._servers: Dict[str, Dict[str, Any]] = {}
        self._tools_cache: Dict[str, List[Dict]] = {}
        self._last_refresh: Optional[datetime] = None

    def register_server(self, name: str, config: Dict[str, Any], tools: Optional[List[Dict]] = None):
        self._servers[name] = {
            "name": name,
            "config": config,
            "registered_at": datetime.now().isoformat(),
            "active": True,
        }
        if tools:
            self._tools_cache[name] = tools
        logger.info(f"Registered MCP server: {name} ({len(tools or [])} tools)")

    def unregister_server(self, name: str):
        self._servers.pop(name, None)
        self._tools_cache.pop(name, None)
        logger.info(f"Unregistered MCP server: {name}")

    def update_tools(self, name: str, tools: List[Dict]):
        self._tools_cache[name] = tools
        if name in self._servers:
            self._servers[name]["active"] = True

    def get_active_servers(self) -> List[str]:
        return [name for name, info in self._servers.items() if info.get("active", False)]

    def get_all_tools(self) -> List[Dict]:
        all_tools = []
        seen = set()
        
        for server_name in self.get_active_servers():
            for tool in self._tools_cache.get(server_name, []):
                # Prefix tool name with server name (matching ClaudeService convention)
                tool_name = f"{server_name}_{tool['name']}"
                if tool_name in seen:
                    continue
                seen.add(tool_name)
                
                all_tools.append({
                    "name": tool_name,
                    "description": f"[{server_name}] {tool.get('description', '')}",
                    "input_schema": tool.get("input_schema", tool.get("inputSchema", {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    })),
                })
        
        return all_tools

    def get_tools_for_server(self, server_name: str) -> List[Dict]:
        return self._tools_cache.get(server_name, [])

    def get_tool_names(self) -> List[str]:
        return [t["name"] for t in self.get_all_tools()]

    def get_agent_sdk_configs(self) -> List[Dict]:
        configs = []
        for name in self.get_active_servers():
            server_info = self._servers.get(name, {})
            config = server_info.get("config", {})
            if config.get("command"):
                configs.append({
                    "name": name,
                    "command": config["command"],
                    "args": config.get("args", []),
                    "env": config.get("env", {}),
                })
        return configs

    async def refresh_from_mcp_client(self):
        try:
            from services.mcp_client import get_mcp_client
            from services.mcp_service import MCPService
            
            mcp_client = get_mcp_client()
            if not mcp_client:
                logger.warning("MCP client not available for registry refresh")
                return
            
            # Get tools from all connected servers
            tools_dict = await mcp_client.list_tools()
            
            for server_name, server_tools in tools_dict.items():
                # Convert tool objects to dicts if needed
                tools = []
                for tool in server_tools:
                    if isinstance(tool, dict):
                        tools.append(tool)
                    else:
                        # Convert from MCP tool object
                        input_schema = getattr(tool, "inputSchema", {})
                        if hasattr(input_schema, "model_dump"):
                            input_schema = input_schema.model_dump()
                        elif not isinstance(input_schema, dict):
                            input_schema = dict(input_schema) if input_schema else {}
                        
                        tools.append({
                            "name": getattr(tool, "name", "unknown"),
                            "description": getattr(tool, "description", ""),
                            "inputSchema": input_schema,
                        })
                
                # Get server config from MCPService
                config = {}
                mcp_service = mcp_client.mcp_service
                if mcp_service and server_name in mcp_service.servers:
                    server = mcp_service.servers[server_name]
                    config = {
                        "command": server.command,
                        "args": server.args,
                        "env": server.env,
                    }
                
                self.register_server(server_name, config, tools)
            
            self._last_refresh = datetime.now()
            logger.info(f"Registry refreshed: {len(self._servers)} servers, "
                       f"{sum(len(t) for t in self._tools_cache.values())} total tools")
            
        except Exception as e:
            logger.error(f"Error refreshing MCP registry: {e}")

    def get_summary(self) -> Dict[str, Any]:
        return {
            "servers": len(self._servers),
            "active_servers": len(self.get_active_servers()),
            "total_tools": sum(len(t) for t in self._tools_cache.values()),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
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
_mcp_registry: Optional[MCPRegistry] = None


def get_mcp_registry() -> MCPRegistry:
    global _mcp_registry
    if _mcp_registry is None:
        _mcp_registry = MCPRegistry()
    return _mcp_registry
