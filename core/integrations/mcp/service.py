"""MCP service for managing MCP servers."""

import json
import logging
import os
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from core.config import vigil_path
from core.detections.detection_rules_service import DetectionRulesService
from core.integrations.integration_bridge_service import IntegrationBridgeService
from core.integrations.mcp.child_env import ca_bundle_env
from core.secrets import get_secret

logger = logging.getLogger(__name__)


# Matches ${VAR_NAME} placeholders in mcp-config.json values/args. Anchored
# to uppercase+underscore+digits so we don't pick up things like
# ${workspaceFolder} (filtered explicitly below regardless).
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Placeholders that are path sentinels, not credentials — never treat as
# required env vars.
_PLACEHOLDER_BLACKLIST = {"workspaceFolder", "HOME", "PYTHONPATH", "VIGIL_DIR"}


def extract_required_env_vars(
    raw_env: Dict[str, str], raw_args: List[str]
) -> List[str]:
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
                            timeout=2,
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
                            [
                                "tasklist",
                                "/FI",
                                "IMAGENAME eq python.exe",
                                "/FO",
                                "CSV",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=2,
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

    # A filename, not a path: resolving at class-definition time is what crashed
    # the daemon in #695, and would pin the read path while the write path
    # resolves fresh.
    _STATE_FILENAME = "mcp_server_enabled.json"

    def __init__(
        self,
        project_root: Optional[Path] = None,
        integration_bridge: Optional[IntegrationBridgeService] = None,
        detection_rules: Optional[DetectionRulesService] = None,
    ):
        """
        Initialize the MCP service.

        Args:
            project_root: Optional project root path. Defaults to the repo root,
                which is where ``mcp-config.json`` and ``venv/`` live.
        """
        if project_root is None:
            # core/integrations/mcp/service.py -> repo root is four levels up.
            project_root = Path(__file__).resolve().parents[3]

        self._integration_bridge = integration_bridge or IntegrationBridgeService()
        self._detection_rules = detection_rules or DetectionRulesService()
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
            state_file = vigil_path(self._STATE_FILENAME)
            if state_file.exists():
                with open(state_file, "r") as f:
                    data = json.load(f)
                return data.get("enabled", {})
        except Exception as e:
            logger.warning(f"Could not load MCP enabled state: {e}")
        return {}

    def _save_enabled_state(self) -> None:
        """Persist the enabled/disabled state to disk."""
        try:
            write_path = vigil_path(self._STATE_FILENAME, write=True)
            with open(write_path, "w") as f:
                json.dump({"enabled": self._enabled_servers}, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save MCP enabled state: {e}")

    # Internal/platform servers that should be on by default
    _DEFAULT_ENABLED = {
        "deeptempo-findings",
        "tempo-flow",
        "security-detections",
        "approval",
        "attack-layer",
        # The self-hosted SIEM a hunt reads through telemetry_search -- the
        # customer's own Splunk, the expected telemetry path, not an optional
        # add-on. Safe to default-on: unset ${SPLUNK_*} placeholders leave it
        # dormant (never connects, no error), not failing a boot.
        "splunk-selfhosted",
    }

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

    def _substitute_env_vars(
        self, value: str, env: Optional[Dict[str, str]] = None
    ) -> str:
        """Expand ``${VAR}`` and ``${VAR:-default}`` in a config string.

        Resolves against ``env`` — the environment the child is actually spawned
        with — so anything the spawn site pinned there is seen rather than
        collapsed to an empty string.
        """
        import re

        source: Mapping[str, str] = os.environ if env is None else env  # noqa: ENV001
        pattern = r"\$\{([^}:]+)(?::-((?:\$\{[^}]+\}|[^{}])*))?\}"

        def replace_var(match):
            var_name = match.group(1)
            default = match.group(2)
            env_val = source.get(var_name)  # operator export wins
            if env_val is None:
                env_val = get_secret(var_name)  # UI-set credential, no restart needed
            if env_val is not None:
                return env_val
            if default is not None:
                return self._substitute_env_vars(default, env)
            return ""

        prev = None
        while prev != value:
            prev = value
            value = re.sub(pattern, replace_var, value)

        return value

    def _detect_server_type(self, args: List[str]) -> str:
        """
        Detect if a server is FastMCP or stdio-based by checking the module path.

        FastMCP servers: deeptempo_findings
        Stdio servers: All others (designed for advanced MCP integration)
        """
        for arg in args:
            # Every in-repo server lives under tools/ (#632 vendored the four
            # that were a submodule into tools/mcp/).
            if "." in arg and arg.startswith("tools"):
                fastmcp_tools = ["deeptempo_findings"]
                for fastmcp in fastmcp_tools:
                    if fastmcp in arg:
                        return "fastmcp"
                return "stdio"
        return "unknown"

    def reload_server_configs(self) -> None:
        """Rebuild server configs so a connectorUrl saved after startup is
        re-substituted into the init-time-cached spawn args."""
        self._initialize_servers()

    def _initialize_servers(self):
        """
        Initialize MCP server configurations from mcp-config.json.

        Loads server configurations dynamically from the mcp-config.json file
        to ensure consistency with MCP integration workflows.
        Also includes servers for enabled integrations.
        """
        python_exe_str = str(self.python_exe)
        project_path_str = str(self.project_root)

        # Resolve ${<ID>_MCP_URL} placeholders from integration connectorUrls
        # (see derive_remote_mcp_env). Best-effort.
        try:
            self._integration_bridge.derive_remote_mcp_env()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("remote MCP env derivation skipped: %s", e)

        # Load servers from mcp-config.json
        mcp_config_path = self.project_root / "mcp-config.json"
        server_configs = []

        if mcp_config_path.exists():
            try:
                with open(mcp_config_path, "r") as f:
                    mcp_config = json.load(f)

                for server_name, server_config in mcp_config.get(
                    "mcpServers", {}
                ).items():
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
                    # Inherit the backend's environment so servers that need
                    # runtime config not declared in mcp-config.json can connect
                    # — notably the POSTGRES_* vars DatabaseService reads for
                    # case/DB tools (deeptempo-findings). Declared config env
                    # entries still take precedence. Required-credential
                    # detection scans the raw config above, not this spawn env,
                    # so dormancy behavior is unchanged.
                    env = os.environ.copy()  # noqa: ENV001 - MCP child env
                    # An mcp-config.json entry may refer to ${VIGIL_DIR}; unset, it
                    # would substitute to "" and root child paths at "/".
                    env.setdefault("VIGIL_DIR", str(vigil_path()))
                    # httpx ignores REQUESTS_CA_BUNDLE, so inheriting it is
                    # not enough.
                    env.update(ca_bundle_env())
                    env.update(
                        {
                            k: self._substitute_env_vars(v, env)
                            for k, v in raw_env_strs.items()
                        }
                    )
                    env["PYTHONPATH"] = project_path_str

                    # Get args and perform environment variable substitution
                    raw_args = list(server_config.get("args") or [])
                    args = [self._substitute_env_vars(arg, env) for arg in raw_args]

                    # Capture declared credential placeholders *before*
                    # substitution collapses missing vars to empty strings
                    # — used by mcp_client to short-circuit connect when
                    # required credentials aren't set.
                    required_env_vars = extract_required_env_vars(
                        raw_env_strs, raw_args
                    )

                    server_configs.append(
                        {
                            "name": server_name,
                            "command": command,
                            "args": args,
                            "cwd": cwd,
                            "env": env,
                            "server_type": self._detect_server_type(args),
                            "required_env_vars": required_env_vars,
                        }
                    )

                logger.info(
                    f"Loaded {len(server_configs)} servers from mcp-config.json"
                )
            except Exception as e:
                logger.error(f"Error loading mcp-config.json: {e}")
                # Fall back to default servers if config loading fails
                server_configs = self._get_default_servers(
                    python_exe_str, project_path_str
                )
        else:
            logger.warning("mcp-config.json not found, using default servers")
            server_configs = self._get_default_servers(python_exe_str, project_path_str)

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
            dynamic_env = self._detection_rules.get_mcp_env_vars()

            if dynamic_env:
                # Override static env vars with dynamic ones
                config["env"] = config.get("env", {}).copy()
                config["env"].update(dynamic_env)
                logger.info(
                    f"Enriched security-detections env with {len(dynamic_env)} dynamic vars: {list(dynamic_env.keys())}"
                )
            else:
                logger.info(
                    "No dynamic env vars from DetectionRulesService (no ready sources)"
                )
        except Exception as e:
            logger.warning(f"Could not enrich security-detections env vars: {e}")

        return config

    def _get_default_servers(
        self, python_exe_str: str, project_path_str: str
    ) -> List[Dict]:
        """Get default server configurations if mcp-config.json is not available."""
        return [
            {
                "name": "deeptempo-findings",
                "command": python_exe_str,
                "args": ["-m", "tools.deeptempo_findings"],
                "cwd": project_path_str,
                "env": {"PYTHONPATH": project_path_str},
                "server_type": "fastmcp",
            }
        ]

    # NOTE: the former `start_server` / `start_all` / `stop_all` methods were
    # removed when the MCP enable toggle became the single runtime lever.
    # They were Popen-subprocess monitors that explicitly refused stdio
    # servers (every server in mcp-config.json is stdio), so they never
    # worked for users anyway. Runtime connect/disconnect is now owned by
    # core.integrations.mcp.client.connect_to_server / disconnect_from_server.

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
            with open(log_path, "r") as f:
                all_lines = f.readlines()
                if not all_lines:
                    return f"Log file is empty. Server may not have started yet.\n\nLog path: {log_path}"
                return "".join(all_lines[-lines:])
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
