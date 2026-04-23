"""MCP service for managing MCP servers."""

import subprocess
import platform
import logging
import json
import re
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime
import os

logger = logging.getLogger(__name__)


# Matches ${VAR_NAME} placeholders in mcp-config.json values/args. Anchored
# to uppercase+underscore+digits so we don't pick up things like
# ${workspaceFolder} (filtered explicitly below regardless).
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Placeholders that are path sentinels, not credentials — never treat as
# required env vars.
_PLACEHOLDER_BLACKLIST = {"workspaceFolder", "HOME", "PYTHONPATH"}


def extract_required_env_vars(raw_env: Dict[str, str], raw_args: List[str]) -> List[str]:
    """Collect every ``${VAR}`` placeholder referenced by a server config.

    Scans the raw (pre-substitution) ``env`` values and ``args`` entries
    from ``mcp-config.json``. Returns a deduplicated, sorted list of
    placeholder names. These are treated as required by
    ``mcp_client.connect_to_server`` — if any resolve to empty, the
    server is considered dormant-by-design (not a connect failure).

    Limitation (documented for follow-ups): this infers requirements
    from the config file. A server whose process quietly needs a
    credential that isn't referenced via ``${…}`` is invisible to us
    and will fall through to the regular connect path.
    """
    found: set[str] = set()
    for value in list((raw_env or {}).values()) + list(raw_args or []):
        if not isinstance(value, str):
            continue
        for m in _ENV_PLACEHOLDER_RE.finditer(value):
            name = m.group(1)
            if name in _PLACEHOLDER_BLACKLIST:
                continue
            found.add(name)
    return sorted(found)


class MCPServer:
    """Represents an MCP server process."""

    def __init__(
        self,
        name: str,
        command: str,
        args: List[str],
        cwd: str,
        env: Dict[str, str],
        server_type: str = "unknown",
        required_env_vars: Optional[List[str]] = None,
    ):
        self.name = name
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self.status = "stopped"
        self.start_time: Optional[datetime] = None
        self.server_type = server_type  # "fastmcp" or "stdio"
        # Credential placeholders declared in mcp-config.json for this
        # server. Read by mcp_client.connect_to_server at connect time.
        self.required_env_vars: List[str] = list(required_env_vars or [])
    
    def start(self) -> bool:
        """Start the MCP server."""
        if self.process is not None:
            logger.warning(f"Server {self.name} is already running")
            return False
        
        try:
            # Prepare environment
            env = os.environ.copy()
            env.update(self.env)
            
            # Start process
            self.process = subprocess.Popen(
                [self.command] + self.args,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            self.status = "running"
            self.start_time = datetime.now()
            logger.info(f"Started MCP server: {self.name}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to start MCP server {self.name}: {e}")
            self.status = "error"
            return False
    
    def stop(self) -> bool:
        """Stop the MCP server."""
        if self.process is None:
            return True
        
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            
            self.process = None
            self.status = "stopped"
            self.start_time = None
            logger.info(f"Stopped MCP server: {self.name}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to stop MCP server {self.name}: {e}")
            return False
    
    def is_running(self) -> bool:
        """Check if the server is running."""
        # First check if we have a process object and it's still alive
        if self.process is not None:
            if self.process.poll() is None:
                # Process is still running
                return True
            else:
                # Process has terminated
                self.status = "stopped"
                self.process = None
                return False
        
        # If no process object, check if the process is running externally
        # by checking for the process by command line arguments
        try:
            # Extract module name from args (e.g., "tools.deeptempo_findings" -> "deeptempo_findings")
            module_name = None
            for arg in self.args:
                if arg.startswith("tools."):
                    parts = arg.split(".")
                    if len(parts) >= 2:
                        module_name = parts[1]
                    break
            
            if module_name:
                # On Unix systems (macOS, Linux), use pgrep
                if platform.system() != "Windows":
                    try:
                        result = subprocess.run(
                            ["pgrep", "-f", f"tools.*{module_name}"],
                            capture_output=True,
                            text=True,
                            timeout=2
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            self.status = "running"
                            return True
                    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                        pass
                else:
                    # On Windows, use tasklist with findstr
                    try:
                        result = subprocess.run(
                            ["tasklist", "/FI", f"IMAGENAME eq python.exe", "/FO", "CSV"],
                            capture_output=True,
                            text=True,
                            timeout=2
                        )
                        if result.returncode == 0 and module_name in result.stdout:
                            self.status = "running"
                            return True
                    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                        pass
        except Exception as e:
            logger.debug(f"Error checking external process status: {e}")
        
        return False
    
    def get_status(self) -> str:
        """Get server status."""
        if self.server_type == "stdio":
            return "stdio (MCP integration)"
        if self.is_running():
            return "running"
        return self.status
    
    def get_log_path(self) -> Path:
        """Get the log file path for this server."""
        # Keep hyphens as servers log to files with hyphens (e.g., deeptempo-findings.log)
        return Path(f"/tmp/{self.name}.log")


class MCPService:
    """Service for managing MCP servers."""
    
    # Path to persist enabled/disabled state for each MCP server
    _STATE_FILE = Path.home() / ".deeptempo" / "mcp_server_enabled.json"
    
    def __init__(self, project_root: Optional[Path] = None):
        """
        Initialize the MCP service.
        
        Args:
            project_root: Optional project root path. Defaults to parent of services directory.
        """
        if project_root is None:
            project_root = Path(__file__).parent.parent
        
        self.project_root = Path(project_root)
        self.venv_path = self.project_root / "venv"
        
        # Determine Python executable
        if platform.system() == "Windows":
            self.python_exe = self.venv_path / "Scripts" / "python.exe"
        else:
            self.python_exe = self.venv_path / "bin" / "python"
        
        # Load enabled state (servers default to disabled)
        self._enabled_servers: Dict[str, bool] = self._load_enabled_state()
        
        # Initialize servers
        self.servers: Dict[str, MCPServer] = {}
        self._initialize_servers()
    
    # ---- Enabled / Disabled state persistence ----
    
    def _load_enabled_state(self) -> Dict[str, bool]:
        """Load the enabled/disabled state from disk. Returns empty dict if no file."""
        try:
            if self._STATE_FILE.exists():
                with open(self._STATE_FILE, "r") as f:
                    data = json.load(f)
                return data.get("enabled", {})
        except Exception as e:
            logger.warning(f"Could not load MCP enabled state: {e}")
        return {}
    
    def _save_enabled_state(self) -> None:
        """Persist the enabled/disabled state to disk."""
        try:
            self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self._STATE_FILE, "w") as f:
                json.dump({"enabled": self._enabled_servers}, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save MCP enabled state: {e}")
    
    # Internal/platform servers that should be on by default
    _DEFAULT_ENABLED = {"deeptempo-findings", "tempo-flow", "security-detections", "approval", "attack-layer"}

    def is_server_enabled(self, server_name: str) -> bool:
        """Check whether a server is enabled. Internal platform servers default to True; all others default to False."""
        return self._enabled_servers.get(
            server_name,
            server_name in self._DEFAULT_ENABLED,
        )
    
    def set_server_enabled(self, server_name: str, enabled: bool) -> bool:
        """
        Enable or disable a server and persist the change.
        
        Returns True if the server exists, False otherwise.
        """
        if server_name not in self.servers:
            return False
        self._enabled_servers[server_name] = enabled
        self._save_enabled_state()
        logger.info(f"Server '{server_name}' {'enabled' if enabled else 'disabled'}")
        return True
    
    def get_all_enabled_states(self) -> Dict[str, bool]:
        """Return a dict of server_name -> enabled for every known server."""
        return {name: self.is_server_enabled(name) for name in self.servers}
    
    def _substitute_env_vars(self, value: str) -> str:
        """
        Substitute environment variables in a string.
        Supports ${VAR_NAME} format.
        
        Args:
            value: String that may contain environment variable references
            
        Returns:
            String with environment variables substituted
        """
        import re
        
        # Pattern to match ${VAR_NAME}
        pattern = r'\$\{([^}]+)\}'
        
        def replace_var(match):
            var_name = match.group(1)
            # Try to get from environment, return empty string if not found
            return os.environ.get(var_name, '')
        
        return re.sub(pattern, replace_var, value)
    
    def _detect_server_type(self, args: List[str]) -> str:
        """
        Detect if a server is FastMCP or stdio-based by checking the module path.
        
        FastMCP servers: deeptempo_findings
        Stdio servers: All others (designed for advanced MCP integration)
        """
        for arg in args:
            # Check both old tools/ and new mcp-servers/servers/ paths
            if ("." in arg and arg.startswith("tools")) or "mcp-servers/servers/" in arg:
                fastmcp_tools = ["deeptempo_findings"]
                for fastmcp in fastmcp_tools:
                    if fastmcp in arg:
                        return "fastmcp"
                return "stdio"
        return "unknown"
    
    def _initialize_servers(self):
        """
        Initialize MCP server configurations from mcp-config.json.
        
        Loads server configurations dynamically from the mcp-config.json file
        to ensure consistency with MCP integration workflows.
        Also includes servers for enabled integrations.
        """
        python_exe_str = str(self.python_exe)
        project_path_str = str(self.project_root)
        
        # Load servers from mcp-config.json
        mcp_config_path = self.project_root / "mcp-config.json"
        server_configs = []
        
        if mcp_config_path.exists():
            try:
                with open(mcp_config_path, 'r') as f:
                    mcp_config = json.load(f)
                    
                for server_name, server_config in mcp_config.get("mcpServers", {}).items():
                    # Skip comment keys
                    if server_name.startswith("_comment"):
                        continue
                        
                    # Convert config format from mcp-config.json to our internal format
                    command = server_config.get("command", "python")
                    
                    # Use venv python if command is just "python" or "python3"
                    if command in ["python", "python3"]:
                        command = python_exe_str
                    
                    # Get cwd, replace ${workspaceFolder} with actual path
                    cwd = server_config.get("cwd", project_path_str)
                    if "${workspaceFolder}" in cwd:
                        cwd = cwd.replace("${workspaceFolder}", project_path_str)
                    
                    # Get environment variables and substitute ${VAR_NAME} patterns
                    raw_env_strs = {
                        k: str(v)
                        for k, v in (server_config.get("env") or {}).items()
                        if not k.startswith("_")
                    }
                    env = {
                        k: self._substitute_env_vars(v)
                        for k, v in raw_env_strs.items()
                    }
                    env["PYTHONPATH"] = project_path_str

                    # Get args and perform environment variable substitution
                    raw_args = list(server_config.get("args") or [])
                    args = [self._substitute_env_vars(arg) for arg in raw_args]

                    # Capture declared credential placeholders *before*
                    # substitution collapses missing vars to empty strings
                    # — used by mcp_client to short-circuit connect when
                    # required credentials aren't set.
                    required_env_vars = extract_required_env_vars(
                        raw_env_strs, raw_args
                    )

                    server_configs.append({
                        "name": server_name,
                        "command": command,
                        "args": args,
                        "cwd": cwd,
                        "env": env,
                        "server_type": self._detect_server_type(args),
                        "required_env_vars": required_env_vars,
                    })
                    
                logger.info(f"Loaded {len(server_configs)} servers from mcp-config.json")
            except Exception as e:
                logger.error(f"Error loading mcp-config.json: {e}")
                # Fall back to default servers if config loading fails
                server_configs = self._get_default_servers(python_exe_str, project_path_str)
        else:
            logger.warning("mcp-config.json not found, using default servers")
            server_configs = self._get_default_servers(python_exe_str, project_path_str)
        
        # Add servers for enabled integrations using the integration bridge
        try:
            from services.integration_bridge_service import get_integration_bridge
            
            bridge = get_integration_bridge()
            enabled_servers = bridge.get_enabled_servers()
            
            # Get list of already loaded server names to avoid duplicates
            loaded_server_names = [s['name'] for s in server_configs]
            
            for server_name, server_info in enabled_servers.items():
                # Skip if already loaded from mcp-config.json
                if server_name in loaded_server_names:
                    logger.info(f"Server '{server_name}' already loaded from mcp-config.json, skipping dynamic load")
                    continue
                
                integration_id = server_info['integration_id']
                env_vars = server_info['env_vars']
                
                # Get module path for this integration
                module_path = bridge.get_server_module_path(integration_id)
                if not module_path:
                    logger.warning(f"No module path found for integration '{integration_id}'")
                    continue
                
                # Prepare environment variables
                env = env_vars.copy()
                env["PYTHONPATH"] = project_path_str
                
                # Create server configuration
                server_config = {
                    "name": server_name,
                    "command": python_exe_str,
                    "args": ["-m", module_path],
                    "cwd": project_path_str,
                    "env": env,
                    "server_type": "stdio"
                }
                
                server_configs.append(server_config)
                logger.info(f"Loaded dynamic integration server: {server_name} for '{integration_id}'")
        
        except ImportError as e:
            logger.warning(f"Could not import integration bridge service: {e}")
        except Exception as e:
            logger.warning(f"Error loading dynamic integration servers: {e}")
        
        # Dynamically update security-detections server env vars from DetectionRulesService
        for config in server_configs:
            if config["name"] == "security-detections":
                config = self._enrich_security_detections_env(config)
            server = MCPServer(**config)
            self.servers[config["name"]] = server
    
    def _enrich_security_detections_env(self, config: Dict) -> Dict:
        """
        Enrich the security-detections MCP server config with dynamic env vars
        from DetectionRulesService. This allows the MCP server to pick up
        newly added/removed rule sources without manual config editing.
        """
        try:
            from services.detection_rules_service import get_detection_rules_service
            
            detection_service = get_detection_rules_service()
            dynamic_env = detection_service.get_mcp_env_vars()
            
            if dynamic_env:
                # Override static env vars with dynamic ones
                config["env"] = config.get("env", {}).copy()
                config["env"].update(dynamic_env)
                logger.info(f"Enriched security-detections env with {len(dynamic_env)} dynamic vars: {list(dynamic_env.keys())}")
            else:
                logger.info("No dynamic env vars from DetectionRulesService (no ready sources)")
        except Exception as e:
            logger.warning(f"Could not enrich security-detections env vars: {e}")
        
        return config
    
    def _get_default_servers(self, python_exe_str: str, project_path_str: str) -> List[Dict]:
        """Get default server configurations if mcp-config.json is not available."""
        return [
            {
                "name": "deeptempo-findings",
                "command": python_exe_str,
                "args": ["-m", "tools.deeptempo_findings"],
                "cwd": project_path_str,
                "env": {"PYTHONPATH": project_path_str},
                "server_type": "fastmcp"
            }
        ]
    
    # NOTE: the former `start_server` / `start_all` / `stop_all` methods were
    # removed when the MCP enable toggle became the single runtime lever.
    # They were Popen-subprocess monitors that explicitly refused stdio
    # servers (every server in mcp-config.json is stdio), so they never
    # worked for users anyway. Runtime connect/disconnect is now owned by
    # services.mcp_client.connect_to_server / disconnect_from_server.

    def stop_server(self, server_name: str) -> bool:
        """Stop a Popen-managed server if one was spawned.

        Kept for completeness: a stdio server never gets a Popen child via
        this class (it's driven by the MCP SDK's ``stdio_client`` through
        ``mcp_client``), so for the current config this is effectively a
        no-op. Still called defensively from ``PUT /enabled`` when a
        non-stdio ``running`` status is observed.
        """
        if server_name not in self.servers:
            logger.error(f"Unknown server: {server_name}")
            return False
        return self.servers[server_name].stop()


    def get_server_status(self, server_name: str) -> Optional[str]:
        """
        Get the status of an MCP server.
        
        Args:
            server_name: Name of the server.
        
        Returns:
            Status string or None if server not found.
        """
        if server_name not in self.servers:
            return None
        
        return self.servers[server_name].get_status()
    
    def get_all_statuses(self) -> Dict[str, str]:
        """
        Get status of all servers.
        
        Returns:
            Dictionary mapping server names to status strings.
        """
        statuses = {}
        for name, server in self.servers.items():
            statuses[name] = server.get_status()
        return statuses
    
    def get_server_log(self, server_name: str, lines: int = 100) -> str:
        """
        Get log content for a server.
        
        Args:
            server_name: Name of the server.
            lines: Number of lines to retrieve (from end).
        
        Returns:
            Log content as string.
        """
        if server_name not in self.servers:
            return ""
        
        log_path = self.servers[server_name].get_log_path()
        
        if not log_path.exists():
            return f"Log file not yet created. Start the server to generate logs.\n\nExpected log path: {log_path}"
        
        try:
            with open(log_path, 'r') as f:
                all_lines = f.readlines()
                if not all_lines:
                    return f"Log file is empty. Server may not have started yet.\n\nLog path: {log_path}"
                return ''.join(all_lines[-lines:])
        except Exception as e:
            return f"Error reading log: {e}"
    
    def test_server(self, server_name: str) -> bool:
        """
        Test if a server is responding.
        
        Args:
            server_name: Name of the server to test.
        
        Returns:
            True if server appears to be running, False otherwise.
        """
        if server_name not in self.servers:
            return False
        
        server = self.servers[server_name]
        return server.is_running()
    
    def list_servers(self) -> List[str]:
        """
        List all available servers.
        
        Returns:
            List of server names.
        """
        return list(self.servers.keys())

