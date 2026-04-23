"""MCP client service for connecting to MCP servers and using their tools with persistent connections."""

import asyncio
import json
import logging
import os
from typing import Optional, Dict, List, Any, Tuple, TYPE_CHECKING
from pathlib import Path
import platform
import threading

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    try:
        # Try alternative import path
        from mcp.client import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        MCP_AVAILABLE = True
    except ImportError:
        MCP_AVAILABLE = False
        # Define dummy types for when MCP is not available
        if TYPE_CHECKING:
            from mcp import ClientSession, StdioServerParameters
        else:
            ClientSession = Any
            StdioServerParameters = Any

from services.mcp_service import MCPService

logger = logging.getLogger(__name__)


class PersistentServerSession:
    """Manages a persistent connection to an MCP server."""
    
    def __init__(self, server_name: str, server_params):
        self.server_name = server_name
        self.server_params = server_params
        self.session: Optional[ClientSession] = None
        self.read_stream = None
        self.write_stream = None
        self.stdio_context = None
        self.session_context = None
        self.is_connected = False
        self.lock = asyncio.Lock()
    
    async def connect(self) -> bool:
        """Establish persistent connection to the server."""
        async with self.lock:
            if self.is_connected and self.session:
                return True
            
            try:
                # Create stdio client connection
                self.stdio_context = stdio_client(self.server_params)
                self.read_stream, self.write_stream = await self.stdio_context.__aenter__()
                
                # Create session
                self.session_context = ClientSession(self.read_stream, self.write_stream)
                self.session = await self.session_context.__aenter__()
                
                # Initialize session
                await self.session.initialize()
                
                self.is_connected = True
                logger.info(f"✓ Established persistent connection to {self.server_name}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to connect to {self.server_name}: {e}")
                await self._cleanup()
                return False
    
    async def disconnect(self):
        """Disconnect from the server."""
        async with self.lock:
            await self._cleanup()
    
    async def _cleanup(self):
        """Internal cleanup method (must be called with lock held)."""
        try:
            if self.session_context:
                try:
                    await self.session_context.__aexit__(None, None, None)
                except Exception:
                    pass
            
            if self.stdio_context:
                try:
                    await self.stdio_context.__aexit__(None, None, None)
                except Exception:
                    pass
            
            self.session = None
            self.session_context = None
            self.stdio_context = None
            self.read_stream = None
            self.write_stream = None
            self.is_connected = False
            
        except Exception as e:
            logger.debug(f"Error during cleanup of {self.server_name}: {e}")
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool using the persistent session."""
        async with self.lock:
            if not self.is_connected or not self.session:
                # Try to reconnect
                logger.warning(f"Session not connected for {self.server_name}, attempting to reconnect...")
                if not await self._reconnect_internal():
                    raise RuntimeError(f"Failed to connect to {self.server_name}")
            
            try:
                result = await self.session.call_tool(tool_name, arguments)
                
                # Convert result to dictionary
                content_list = []
                for content_item in result.content:
                    if hasattr(content_item, 'text'):
                        content_list.append({"type": "text", "text": content_item.text})
                    elif hasattr(content_item, 'type'):
                        content_list.append({"type": str(content_item.type), "text": str(content_item)})
                    else:
                        content_list.append({"type": "text", "text": str(content_item)})
                
                return {
                    "error": result.isError if hasattr(result, 'isError') else False,
                    "content": content_list
                }
                
            except Exception as e:
                logger.error(f"Tool call failed for {self.server_name}.{tool_name}: {e}")
                # Mark as disconnected and try to reconnect on next call
                self.is_connected = False
                raise
    
    async def _reconnect_internal(self) -> bool:
        """Internal reconnect (must be called with lock held)."""
        await self._cleanup()
        return await self._connect_internal()
    
    async def _connect_internal(self) -> bool:
        """Internal connect (must be called with lock held)."""
        try:
            self.stdio_context = stdio_client(self.server_params)
            self.read_stream, self.write_stream = await self.stdio_context.__aenter__()
            
            self.session_context = ClientSession(self.read_stream, self.write_stream)
            self.session = await self.session_context.__aenter__()
            
            await self.session.initialize()
            
            self.is_connected = True
            return True
            
        except Exception as e:
            logger.error(f"Reconnect failed for {self.server_name}: {e}")
            await self._cleanup()
            return False


class MCPClient:
    """Client for connecting to MCP servers and using their tools with persistent connections."""
    
    def __init__(self, mcp_service: MCPService):
        """
        Initialize MCP client with persistent connection support.

        Args:
            mcp_service: MCPService instance for managing server processes
        """
        self.mcp_service = mcp_service
        self.persistent_sessions: Dict[str, PersistentServerSession] = {}
        self.tools_cache: Dict[str, List[Dict]] = {}
        self._connection_locks: Dict[str, threading.Lock] = {}  # Locks per server to prevent concurrent connections
        # Populated by connect_to_server — string reason on failure, and a
        # structured list of env var names when credentials are missing.
        self.last_errors: Dict[str, str] = {}
        self.last_missing_credentials: Dict[str, List[str]] = {}
    
    async def connect_to_server(self, server_name: str, persistent: bool = True) -> bool:
        """
        Connect to an MCP server, cache its tools, and optionally maintain persistent connection.

        Only connects if the server is enabled in the MCP service. On failure, the
        exception message is recorded on ``self.last_errors[server_name]`` so the
        Settings → MCP UI can surface *why* a connection failed (missing binary,
        credentials, package not installed) instead of a generic "Failed to connect".

        Args:
            server_name: Name of the server to connect to
            persistent: If True, maintain persistent connection for reuse

        Returns:
            True if successful, False otherwise
        """
        # Defensive init for deployments that upgraded without reinstantiating
        # the client — keeps the legacy __init__ compatible.
        if not hasattr(self, "last_errors"):
            self.last_errors: Dict[str, str] = {}
        if not hasattr(self, "last_missing_credentials"):
            self.last_missing_credentials: Dict[str, List[str]] = {}
        # Clear any stale state from a prior attempt so the UI always
        # reflects the most recent connect.
        self.last_errors.pop(server_name, None)
        self.last_missing_credentials.pop(server_name, None)

        if not MCP_AVAILABLE:
            logger.error("MCP SDK not available")
            self.last_errors[server_name] = "MCP SDK not installed in the backend venv"
            return False

        if server_name not in self.mcp_service.servers:
            logger.error(f"Unknown server: {server_name}")
            self.last_errors[server_name] = "Server not present in mcp-config.json"
            return False

        # Skip disabled servers
        if not self.mcp_service.is_server_enabled(server_name):
            logger.debug(f"Server {server_name} is disabled, skipping connection")
            return False

        # Check if already connected with cached tools
        if server_name in self.persistent_sessions and server_name in self.tools_cache:
            if self.persistent_sessions[server_name].is_connected:
                logger.debug(f"Already connected to {server_name}")
                return True

        server = self.mcp_service.servers[server_name]

        # Credential gate: if the server declared ${VAR} placeholders in
        # its mcp-config.json entry and those env vars resolve empty,
        # short-circuit without spawning a child. This is dormancy by
        # design — per #124's conclusion, pre-configuration is not a
        # failure. The UI's existing "Not Configured" treatment takes
        # over once it sees connected=false + a missing_credentials list.
        missing = self._missing_credentials_for(server)
        if missing:
            msg = f"missing credentials: {', '.join(missing)}"
            self.last_errors[server_name] = msg
            self.last_missing_credentials[server_name] = missing
            logger.info(
                "MCP server %s dormant — waiting on env vars: %s",
                server_name,
                ", ".join(missing),
            )
            return False

        try:
            # Create stdio server parameters
            server_params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=server.env
            )
            
            if persistent:
                # Create persistent session
                if server_name not in self.persistent_sessions:
                    self.persistent_sessions[server_name] = PersistentServerSession(
                        server_name, server_params
                    )
                
                # Connect
                if not await self.persistent_sessions[server_name].connect():
                    return False
                
                # Get tools from the persistent session
                session = self.persistent_sessions[server_name].session
                tools_result = await session.list_tools()
                
            else:
                # Temporary connection just to get tools
                async with stdio_client(server_params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools_result = await session.list_tools()
            
            # Cache tools
            self.tools_cache[server_name] = []
            for tool in tools_result.tools:
                # Get input schema - handle both dict and object formats
                input_schema = tool.inputSchema
                if hasattr(input_schema, 'model_dump'):
                    input_schema = input_schema.model_dump()
                elif hasattr(input_schema, 'dict'):
                    input_schema = input_schema.dict()
                elif not isinstance(input_schema, dict):
                    input_schema = dict(input_schema) if input_schema else {}
                
                # Ensure it's a valid JSON schema
                if not isinstance(input_schema, dict):
                    input_schema = {}
                
                self.tools_cache[server_name].append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": input_schema
                })
            
            logger.info(f"Connected to {server_name}, found {len(self.tools_cache[server_name])} tools")
            return True
        
        except Exception as e:
            # Preserve the exception text so the UI can surface the real
            # reason (e.g. "FileNotFoundError: uvx", "ModuleNotFoundError:
            # mempalace", "missing env var GITHUB_TOKEN") instead of a
            # generic "Failed to connect".
            self.last_errors[server_name] = f"{type(e).__name__}: {e}"
            logger.error(f"Failed to connect to {server_name}: {e}")
            return False

    def _missing_credentials_for(self, server) -> List[str]:
        """Return the subset of a server's required_env_vars that resolve empty.

        Checks both ``os.environ`` and the Vigil secrets manager, so a
        user who saved a credential via the integration wizard (which
        writes to the encrypted store, not the process env) isn't told
        the server is still dormant.
        """
        required = getattr(server, "required_env_vars", None) or []
        if not required:
            return []
        try:
            from backend.secrets_manager import get_secret
        except Exception:  # pragma: no cover — secrets module always present
            get_secret = lambda _name: None  # type: ignore[assignment]

        missing: List[str] = []
        for var in required:
            if os.environ.get(var):
                continue
            if get_secret(var):
                continue
            missing.append(var)
        return missing

    def get_missing_credentials(self, server_name: str) -> Optional[List[str]]:
        """Return the list of unset required env vars from the last connect."""
        return getattr(self, "last_missing_credentials", {}).get(server_name)

    def get_last_error(self, server_name: str) -> Optional[str]:
        """Return the most recent connect-failure reason for a server, if any."""
        return getattr(self, "last_errors", {}).get(server_name)

    # Per-server rate limit between auto-retry attempts. Prevents a
    # misconfigured secret (wrong value, typo) from hammering the MCP
    # child process with connect storms on every /connections/status
    # poll. 15s balances "user saves key and sees it online quickly"
    # with "don't spin up 20 subprocesses a minute on a stuck setup".
    _RETRY_MIN_INTERVAL_S = 15.0

    async def retry_dormant_if_ready(self) -> Dict[str, bool]:
        """Re-attempt connect for any dormant server whose required env
        vars have since resolved (e.g. user saved the credential via the
        integration wizard). Safe to call from a read-path endpoint:
        no-op when nothing's dormant or when creds are still missing.

        Returns a dict ``{server_name: connected_bool}`` recording what
        we actually tried this call — the common case is an empty dict.
        """
        # Defensive init — mirrors connect_to_server's compat shim.
        if not hasattr(self, "_last_retry_at"):
            self._last_retry_at: Dict[str, float] = {}
        if not hasattr(self, "last_missing_credentials"):
            return {}

        import time

        now = time.monotonic()
        attempted: Dict[str, bool] = {}

        # Snapshot the keys — ``last_missing_credentials`` is mutated
        # by ``connect_to_server`` we're about to call.
        candidates = [
            name
            for name, missing in list(self.last_missing_credentials.items())
            if missing
        ]
        for server_name in candidates:
            server = self.mcp_service.servers.get(server_name)
            if server is None:
                continue
            # Cheap precheck — skip unless creds actually resolve now.
            if self._missing_credentials_for(server):
                continue
            # Rate-limit: don't retry the same server more than once
            # per _RETRY_MIN_INTERVAL_S seconds.
            last = self._last_retry_at.get(server_name, 0.0)
            if now - last < self._RETRY_MIN_INTERVAL_S:
                continue
            self._last_retry_at[server_name] = now
            try:
                attempted[server_name] = await self.connect_to_server(
                    server_name, persistent=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dormant-retry for %s raised: %s", server_name, exc
                )
                attempted[server_name] = False
        if attempted:
            connected = sum(1 for v in attempted.values() if v)
            logger.info(
                "Auto-reconnect: %d/%d dormant server(s) came online",
                connected,
                len(attempted),
            )
        return attempted

    async def list_tools(self, server_name: Optional[str] = None) -> Dict[str, List[Dict]]:
        """
        List available tools from MCP servers.
        
        Args:
            server_name: Optional server name to list tools from. If None, lists from all servers.
            
        Returns:
            Dictionary mapping server names to lists of tool definitions
        """
        if not MCP_AVAILABLE:
            return {}
        
        tools = {}
        
        if server_name:
            if server_name in self.tools_cache:
                tools[server_name] = self.tools_cache[server_name]
            else:
                # Try to connect and get tools
                if await self.connect_to_server(server_name):
                    tools[server_name] = self.tools_cache.get(server_name, [])
        else:
            # List tools from all servers
            for name in self.mcp_service.list_servers():
                if name in self.tools_cache:
                    tools[name] = self.tools_cache[name]
                else:
                    # Try to connect
                    if await self.connect_to_server(name):
                        tools[name] = self.tools_cache.get(name, [])
        
        return tools
    
    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """
        Call a tool on an MCP server using persistent connection with timeout.

        Args:
            server_name: Name of the server
            tool_name: Name of the tool to call
            arguments: Tool arguments
            timeout: Timeout in seconds (default: 30)

        Returns:
            Tool result dictionary
        """
        import json as _json
        import time as _time

        if not MCP_AVAILABLE:
            return {"error": "MCP SDK not available", "content": [{"type": "text", "text": "MCP SDK not available"}]}

        if server_name not in self.mcp_service.servers:
            return {"error": f"Unknown server: {server_name}", "content": [{"type": "text", "text": f"Unknown server: {server_name}"}]}

        # OTEL span for transport-level MCP call
        _mcp_span = None
        _mcp_t0 = _time.monotonic()
        try:
            from core.telemetry import get_tracer
            from opentelemetry.trace import SpanKind, StatusCode as _SC
            _mcp_tracer = get_tracer("vigil.services.mcp_client")
            _mcp_span = _mcp_tracer.start_span(
                "mcp.call_tool",
                kind=SpanKind.CLIENT,
                attributes={
                    "mcp.server.name": server_name,
                    "mcp.transport": "stdio",
                    "vigil.tool.name": tool_name,
                    "vigil.tool.input_size": len(_json.dumps(arguments, default=str)),
                },
            )
        except Exception:
            _SC = None

        # Ensure we have a persistent session
        if server_name not in self.persistent_sessions:
            logger.info(f"Creating persistent connection to {server_name}...")
            if not await self.connect_to_server(server_name, persistent=True):
                _err = {
                    "error": True,
                    "content": [{"type": "text", "text": f"Failed to connect to server: {server_name}"}]
                }
                try:
                    if _mcp_span is not None:
                        _mcp_span.set_attribute("vigil.tool.success", False)
                        _mcp_span.end()
                except Exception:
                    pass
                return _err

        persistent_session = self.persistent_sessions[server_name]

        async def _call_tool_persistent():
            try:
                return await persistent_session.call_tool(tool_name, arguments)
            except Exception as e:
                logger.error(f"Error in tool call {tool_name} on {server_name}: {e}")
                raise

        try:
            # Apply timeout
            result = await asyncio.wait_for(_call_tool_persistent(), timeout=timeout)
            try:
                if _mcp_span is not None:
                    is_err = result.get("error", False) if isinstance(result, dict) else False
                    _mcp_span.set_attribute("vigil.tool.success", not is_err)
                    _mcp_span.set_attribute("vigil.tool.output_size", len(_json.dumps(result, default=str)))
                    _mcp_span.set_attribute("vigil.tool.duration_ms", round((_time.monotonic() - _mcp_t0) * 1000, 1))
                    _mcp_span.end()
            except Exception:
                pass
            return result
        except asyncio.TimeoutError:
            logger.error(f"Tool call {tool_name} on {server_name} timed out after {timeout}s")
            try:
                if _mcp_span is not None and _SC is not None:
                    _mcp_span.set_attribute("vigil.tool.success", False)
                    _mcp_span.set_status(_SC.ERROR, f"Timeout after {timeout}s")
                    _mcp_span.end()
            except Exception:
                pass
            return {
                "error": True,
                "content": [{"type": "text", "text": f"Tool call timed out after {timeout} seconds. The MCP server may not be responding."}]
            }
        except Exception as e:
            logger.error(f"Error calling tool {tool_name} on {server_name}: {e}")
            try:
                if _mcp_span is not None:
                    _mcp_span.set_attribute("vigil.tool.success", False)
                    _mcp_span.end()
            except Exception:
                pass
            return {"error": True, "content": [{"type": "text", "text": f"Error: {str(e)}"}]}
    
    def get_tools_for_claude(self) -> List[Dict]:
        """
        Get all available tools formatted for Claude's tool use API.
        
        Returns:
            List of tool definitions in Claude's format
        """
        all_tools = []
        
        for server_name, tools in self.tools_cache.items():
            for tool in tools:
                # Format tool for Claude API
                claude_tool = {
                    "name": f"{server_name}_{tool['name']}",
                    "description": f"[{server_name}] {tool['description']}",
                    "input_schema": tool.get("inputSchema", {})
                }
                all_tools.append(claude_tool)
        
        return all_tools
    
    async def disconnect_from_server(self, server_name: str) -> bool:
        """
        Disconnect from a specific MCP server.
        
        Args:
            server_name: Name of the server to disconnect from
            
        Returns:
            True if successful, False otherwise
        """
        if server_name in self.persistent_sessions:
            try:
                await self.persistent_sessions[server_name].disconnect()
                del self.persistent_sessions[server_name]
                logger.info(f"Disconnected from {server_name}")
                return True
            except Exception as e:
                logger.error(f"Error disconnecting from {server_name}: {e}")
                return False
        return True
    
    async def reconnect_to_server(self, server_name: str) -> bool:
        """
        Reconnect to a specific MCP server.
        
        Args:
            server_name: Name of the server to reconnect to
            
        Returns:
            True if successful, False otherwise
        """
        # Disconnect if already connected
        await self.disconnect_from_server(server_name)
        
        # Reconnect
        return await self.connect_to_server(server_name, persistent=True)
    
    def get_connection_status(self) -> Dict[str, bool]:
        """
        Get connection status for all servers.
        
        Returns:
            Dictionary mapping server names to connection status
        """
        status = {}
        for server_name in self.mcp_service.list_servers():
            if server_name in self.persistent_sessions:
                status[server_name] = self.persistent_sessions[server_name].is_connected
            else:
                status[server_name] = False
        return status
    
    async def close_all(self):
        """Close all persistent MCP server connections and clear cache."""
        logger.info("Closing all MCP server connections...")
        
        # Disconnect all persistent sessions sequentially to avoid context issues
        for server_name in list(self.persistent_sessions.keys()):
            try:
                await self.persistent_sessions[server_name].disconnect()
                logger.info(f"Disconnected from {server_name}")
            except Exception as e:
                logger.error(f"Error disconnecting from {server_name}: {e}")
        
        # Clear all state
        self.persistent_sessions.clear()
        self.tools_cache.clear()
        
        logger.info("All MCP connections closed")


# Global MCP client instance
_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> Optional[MCPClient]:
    """Get or create the global MCP client instance."""
    global _mcp_client
    
    if not MCP_AVAILABLE:
        logger.warning("MCP SDK not available. Install with: pip install mcp")
        return None
    
    if _mcp_client is None:
        mcp_service = MCPService()
        _mcp_client = MCPClient(mcp_service)
    
    return _mcp_client

