"""MCP service for managing MCP servers."""

import json
import logging
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.config import vigil_path
from core.detections.detection_rules_service import DetectionRulesService
from core.integrations.integration_bridge_service import (
    PATH_PLACEHOLDERS,
    VAR_NAME,
    IntegrationBridgeService,
)
from core.integrations.mcp.packaged import installed_launch
from core.secrets import get_secret

logger = logging.getLogger(__name__)

# Stands in for "$" inside an already-substituted value (see _substitute_env_vars).
_LITERAL_DOLLAR = "\x00"


# Matches ${VAR_NAME} placeholders in mcp-config.json values/args. Anchored
# to uppercase+underscore+digits so we don't pick up things like
# ${workspaceFolder} (filtered explicitly below regardless).
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Placeholders that are path sentinels, not credentials — never treat as
# required env vars.
_PLACEHOLDER_BLACKLIST = PATH_PLACEHOLDERS


def extract_required_env_vars(
    raw_env: Dict[str, str],
    raw_args: List[str],
    url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Collect every ``${VAR}`` placeholder referenced by a server config.

    Scans the raw (pre-substitution) ``env`` values and ``args`` entries
    from ``mcp-config.json``, and a remote entry's ``url`` and ``headers``. Returns a deduplicated, sorted list of
    placeholder names. These are treated as required by
    ``mcp_client.connect_to_server`` — if any resolve to empty, the
    server is considered dormant-by-design (not a connect failure).

    Limitation (documented for follow-ups): this infers requirements
    from the config file. A server whose process quietly needs a
    credential that isn't referenced via ``${…}`` is invisible to us
    and will fall through to the regular connect path.
    """
    found: set[str] = set()
    values = [*(raw_env or {}).values(), *(raw_args or []), url]
    for value in values + list((headers or {}).values()):
        if not isinstance(value, str):
            continue
        # "$${VAR}" is a literal for the child to expand, not a placeholder.
        for m in _ENV_PLACEHOLDER_RE.finditer(value.replace("$$", "")):
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
        required_env_vars: Optional[List[str]] = None,
        declared_env: Optional[List[str]] = None,
        remote: bool = False,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        source: bool = False,
        instance_of: Optional[str] = None,
    ):
        self.name = name
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = env
        # Credential placeholders declared in mcp-config.json for this
        # server. Read by mcp_client.connect_to_server at connect time.
        self.required_env_vars: List[str] = list(required_env_vars or [])
        # The env names mcp-config.json declares; ``env`` also carries the
        # backend's own environment, which only a stdio child inherits.
        self.declared_env: List[str] = list(declared_env or [])
        # Reaches a remote server (its raw args run mcp-remote), so its tools'
        # classified definitions are pinned (#5300).
        self.remote = remote
        # A remote entry: no process, reached over streamable HTTP. Both are the
        # raw ${VAR} templates, resolved at each connect (MCPService.remote_target).
        self.url = url
        self.headers: Dict[str, str] = dict(headers or {})
        # A declared source (core.sources): its headers hold {{field}}
        # templates over its own credentials, not ${VAR}.
        self.source = source
        # An instance (#5353): launched from this catalogue entry's launch.
        self.instance_of = instance_of


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
        declared_sources: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        """
        Initialize the MCP service.

        Args:
            project_root: Optional project root path. Defaults to the repo root,
                which is where ``mcp-config.json`` and ``venv/`` live.
            declared_sources: Returns the ``sources.applied`` row. The runner
                and ``verify_catalog`` pass one returning ``{}``, so neither
                reads the database.
        """
        if project_root is None:
            # core/integrations/mcp/service.py -> repo root is four levels up.
            project_root = Path(__file__).resolve().parents[3]

        self._integration_bridge = integration_bridge or IntegrationBridgeService()
        self._detection_rules = detection_rules or DetectionRulesService()
        self._declared_sources = declared_sources or _applied_sources
        # Declared source -> why it is not in the catalog (pending, refused,
        # named like a catalog server), as of the last load_catalog.
        self.source_states: Dict[str, str] = {}
        self.project_root = Path(project_root)
        self.venv_path = self.project_root / "venv"

        # Determine Python executable
        if platform.system() == "Windows":
            self.python_exe = self.venv_path / "Scripts" / "python.exe"
        else:
            self.python_exe = self.venv_path / "bin" / "python"
        if not self.python_exe.is_file():
            self.python_exe = Path(sys.executable)

        # Initialize servers
        self.servers: Dict[str, MCPServer] = {}
        self._initialize_servers()

    # ---- Enabled / Disabled state persistence ----

    @classmethod
    def _load_enabled_state(cls) -> Dict[str, bool]:
        """Load the enabled/disabled state from disk. Returns empty dict if no file."""
        try:
            with open(vigil_path(cls._STATE_FILENAME)) as f:
                return json.load(f).get("enabled", {})
        except FileNotFoundError:
            return {}

    def _save_enabled_state(self, enabled: Dict[str, bool]) -> None:
        """Persist the enabled/disabled state to disk."""
        write_path = vigil_path(self._STATE_FILENAME, write=True)
        # Replaced whole: the other worker and the daemon read it on every check.
        tmp = write_path.with_name(f".{write_path.name}.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump({"enabled": enabled}, f, indent=2)
        os.replace(tmp, write_path)

    # Internal/platform servers that should be on by default.
    #
    # Vigil's own server is not here: it is not a server Vigil connects to but
    # functions in this process, registered by core.integrations.mcp.in_process
    # and reachable whether or not anything is enabled.
    _DEFAULT_ENABLED = {
        "security-detections",
        # The self-hosted SIEM a hunt reads through telemetry_search -- the
        # customer's own Splunk, the expected telemetry path, not an optional
        # add-on. Safe to default-on: it declares no env placeholder (its config
        # comes from Settings via resolve(), #1113), so MCPClient gates it on the
        # resolved config instead. Dormant until Splunk is saved, then it starts.
        "splunk-selfhosted",
    }

    # Declared sources merged by this process's last load_catalog. On by
    # default, since a person accepted each; a switch-off is saved as any other.
    _sources_on: frozenset = frozenset()
    # Instances merged by this process's last load_catalog (#5353).
    _instances: frozenset = frozenset()

    @classmethod
    def enabled_state(cls) -> Dict[str, bool]:
        """Every saved server switch, read once, for a caller asking about many
        servers; pass it to is_server_enabled as ``state``."""
        return cls._load_enabled_state()

    @classmethod
    def is_server_enabled(
        cls, server_name: str, state: Optional[Dict[str, bool]] = None
    ) -> bool:
        """Check whether a server is enabled. Internal platform servers default to True; all others default to False.

        Read from the file on every check: the other API worker and the daemon
        toggle the same file, and share the runner's one copy of each server.
        A class method, so a reader with no service of its own (integration
        health) reads the same file the same way.
        """
        if state is None:
            state = cls._load_enabled_state()
        return state.get(
            server_name,
            server_name in cls._DEFAULT_ENABLED or server_name in cls._sources_on,
        )

    def set_server_enabled(self, server_name: str, enabled: bool) -> bool:
        """
        Enable or disable a server and persist the change.

        Returns True if the server exists, False otherwise.
        """
        if server_name not in self.servers:
            return False
        state = self._load_enabled_state()
        state[server_name] = enabled
        self._save_enabled_state(state)
        logger.info(f"Server '{server_name}' {'enabled' if enabled else 'disabled'}")
        return True

    # The server that answers from detection rules the install has to fetch
    # first. ``POST /api/detection-rules/update-all`` is that fetch.
    DETECTIONS_SERVER = "security-detections"
    FETCH_RULES = "fetch_detection_rules"
    _NO_RULES = "No detection rules have been fetched on this install yet."

    def setup_required(self, server_name: str) -> Optional[str]:
        """Why ``server_name`` cannot start yet, or None when nothing stops it.

        Not a failure and not a missing credential: security-detections
        searches rule repositories, and a fresh install has none until they are
        fetched (#2291). Started anyway it indexes nothing and answers every
        search with nothing. Read fresh on each call, so a fetch made by another
        worker counts here too; the server's env takes the paths it found.
        """
        if server_name != self.DETECTIONS_SERVER:
            return None
        try:
            env = self._detection_rules.saved_mcp_env_vars()
        except Exception as exc:  # noqa: BLE001 - a status read must not raise
            logger.warning("Could not read the detection rule sources: %s", exc)
            env = {}
        if not env:
            return self._NO_RULES
        server = self.servers.get(server_name)
        if server is not None:
            server.env = {**(server.env or {}), **env}
        return None

    def setup_action(self, server_name: str) -> Optional[str]:
        """The one action that sets ``server_name`` up, for a client to offer."""
        return self.FETCH_RULES if server_name == self.DETECTIONS_SERVER else None

    def get_all_enabled_states(self) -> Dict[str, bool]:
        """Return a dict of server_name -> enabled for every known server."""
        state = self._load_enabled_state()
        return {name: self.is_server_enabled(name, state) for name in self.servers}

    def _substitute_env_vars(
        self, value: str, env: Optional[Dict[str, str]] = None
    ) -> str:
        """Expand ``${VAR}`` and ``${VAR:-default}`` in a config string.

        ``$$`` is a literal ``$``, so ``$${VAR}`` reaches the child as
        ``${VAR}`` for it to expand from its own env (keeps secrets off argv).

        Resolves against ``env`` — the environment the child is actually spawned
        with — so anything the spawn site pinned there is seen rather than
        collapsed to an empty string.

        Only the template is expanded. A resolved value is spliced in as plain
        text, so a stored value that itself contains ``${...}`` (a saved
        connector URL, say) cannot name another variable or secret to read.
        """
        source: Mapping[str, str] = os.environ if env is None else env  # noqa: ENV001
        # A default may nest ${VAR} or an escaped $${VAR} (by then "\x00{VAR}").
        pattern = r"\$\{([^}:]+)(?::-((?:[$\x00]\{[^}]+\}|[^{}])*))?\}"

        def replace_var(match):
            var_name = match.group(1)
            default = match.group(2)
            # A non-empty export wins; empty counts as unset, as in bash ${VAR:-d}
            # and everywhere else credentials resolve.
            env_val = source.get(var_name) or get_secret(var_name)
            if env_val:
                return env_val.replace("$", _LITERAL_DOLLAR)
            if default is not None:
                return self._substitute_env_vars(default, env).replace(
                    "$", _LITERAL_DOLLAR
                )
            return ""

        # Repeat only to unwind nested defaults; resolved values carry no "$"
        # until the end, so they are never matched again.
        value = value.replace("$$", _LITERAL_DOLLAR)
        prev = None
        while prev != value:
            prev = value
            value = re.sub(pattern, replace_var, value)

        return value.replace(_LITERAL_DOLLAR, "$")

    def remote_target(self, server: "MCPServer") -> Tuple[str, Dict[str, str]]:
        """A remote entry's URL and headers, resolved now.

        Through ``get_secret`` (the store, then the environment), the order the
        dormancy gate reads them in, so a token saved in Settings after the
        catalog loaded is the one sent. Not the environment first: a save sets
        ``os.environ`` only in the worker that took it, so another worker's
        copy of an earlier value would win there.

        A declared source's URL is literal, and its headers fill ``{{field}}``
        from its own credentials (``SOURCE_<NAME>_<FIELD>``) the same way.
        """
        if server.source:
            from core.sources import TEMPLATE, credential_key

            def credential(field: str) -> str:
                value = (get_secret(credential_key(server.name, field)) or "").strip()
                # Never into a header (h11 would echo it in its error).
                if any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError(f"credential {field} holds control characters")
                return value

            def fill(value: str) -> str:
                return TEMPLATE.sub(lambda m: credential(m.group(1)), value)

            return server.url or "", {k: fill(v) for k, v in server.headers.items()}
        return self._substitute_env_vars(server.url or "", {}), {
            k: self._substitute_env_vars(v, {}) for k, v in server.headers.items()
        }

    def reload_server_configs(self) -> None:
        """Rebuild server configs so a connectorUrl saved after startup is
        re-substituted into the init-time-cached spawn args."""
        self._initialize_servers()

    def _initialize_servers(self):
        """Merge the catalog from mcp-config.json into ``self.servers``.

        A file that does not parse is logged and leaves ``self.servers`` as is.
        """
        try:
            self.servers.update(self.load_catalog())
        except Exception as e:
            logger.error(f"Error loading mcp-config.json: {e}")

    def load_catalog(self) -> Dict[str, "MCPServer"]:
        """Build the server catalog from mcp-config.json without touching
        ``self.servers``. Raises when the file does not parse; a missing file
        is an empty catalog.
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
        # Launched entries as written, for instances to copy (#5353).
        raw_entries: Dict[str, dict] = {}

        if mcp_config_path.exists():
            with open(mcp_config_path, "r") as f:
                mcp_config = json.load(f)

            for server_name, server_config in mcp_config.get("mcpServers", {}).items():
                # Skip comment keys
                if server_name.startswith("_comment"):
                    continue

                if "url" in server_config:
                    # Remote: no process, so no command, args, cwd or launcher.
                    url = str(server_config["url"])
                    headers = {
                        str(k): str(v)
                        for k, v in (server_config.get("headers") or {}).items()
                    }
                    server_configs.append(
                        {
                            "name": server_name,
                            "command": "",
                            "args": [],
                            "cwd": project_path_str,
                            "env": {},
                            "required_env_vars": extract_required_env_vars(
                                {}, [], url, headers
                            ),
                            "url": url,
                            "headers": headers,
                        }
                    )
                    continue

                server_configs.append(
                    self._launched(server_name, server_config, python_exe_str)
                )
                raw_entries[server_name] = server_config

            logger.info(f"Loaded {len(server_configs)} servers from mcp-config.json")
        else:
            logger.warning("mcp-config.json not found")

        # Dynamically update security-detections server env vars from DetectionRulesService
        servers: Dict[str, MCPServer] = {}
        for config in server_configs:
            if config["name"] == "security-detections":
                config = self._enrich_security_detections_env(config)
            servers[config["name"]] = MCPServer(**config)
        servers.update(self._merge_instances(servers, raw_entries, python_exe_str))
        servers.update(self._merge_sources(servers, project_path_str))
        return servers

    def _launched(
        self,
        server_name: str,
        server_config: Dict[str, Any],
        python_exe_str: str,
        renames: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """A launched entry as MCPServer kwargs. ``renames`` rewrites each
        ``${VAR}`` it names before substitution, so an instance reads its own."""
        project_path_str = str(self.project_root)

        def raw(value: Any) -> str:
            if not renames:
                return str(value)
            return VAR_NAME.sub(
                lambda m: "${" + renames.get(m.group(1), m.group(1)), str(value)
            )

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
            k: raw(v)
            for k, v in (server_config.get("env") or {}).items()
            if not k.startswith("_")
        }
        # Substitution reads this process's environment; the server
        # keeps only its declared keys. The rest of the child's env is
        # built at connect time (child_env.stdio_env).
        source = dict(os.environ)  # noqa: ENV001 - substitution source
        # An mcp-config.json entry may refer to ${VIGIL_DIR}; unset, it
        # would substitute to "" and root child paths at "/".
        source.setdefault("VIGIL_DIR", str(vigil_path()))
        env = {k: self._substitute_env_vars(v, source) for k, v in raw_env_strs.items()}

        # Get args and perform environment variable substitution
        raw_args = [raw(a) for a in server_config.get("args") or []]
        args = [self._substitute_env_vars(arg, source) for arg in raw_args]

        # Capture declared credential placeholders *before*
        # substitution collapses missing vars to empty strings
        # — used by mcp_client to short-circuit connect when
        # required credentials aren't set.
        required_env_vars = extract_required_env_vars(raw_env_strs, raw_args)

        # Launch the image's baked copy of a pinned npx/uvx entry
        # instead of downloading it; anything not baked is as declared.
        command, args = installed_launch(command, args)

        return {
            "name": server_name,
            "command": command,
            "args": args,
            "cwd": cwd,
            "env": env,
            "required_env_vars": required_env_vars,
            "declared_env": list(raw_env_strs),
            # Raw args: installed_launch swaps the npx spec for a path.
            "remote": any(str(a).startswith("mcp-remote") for a in raw_args),
        }

    def _merge_instances(
        self,
        catalog: Dict[str, "MCPServer"],
        raw_entries: Dict[str, dict],
        python_exe_str: str,
    ) -> Dict[str, "MCPServer"]:
        """One server per saved instance (#5353): a copy of its base entry's
        launch under the instance's name, reading the instance's own settings.
        A failed read merges none."""
        from core.integrations.integration_bridge_service import (
            instance_renames,
            instances_allowed,
        )

        reader = getattr(self._integration_bridge, "instances", None)
        try:
            saved = reader() if reader is not None else {}
        except Exception as exc:  # noqa: BLE001 - an instance never stops the catalog
            logger.warning("Integration instances not read: %s", exc)
            saved = {}
        merged: Dict[str, MCPServer] = {}
        for name, base in saved.items():
            entry = raw_entries.get(base)
            if name in catalog or entry is None or not instances_allowed(base)[0]:
                logger.warning("Instance %s of %s not merged", name, base)
                continue
            config = self._launched(
                name, entry, python_exe_str, renames=instance_renames(name)
            )
            merged[name] = MCPServer(**config, instance_of=base)
        MCPService._instances = frozenset(merged)
        return merged

    def _merge_sources(
        self, catalog: Dict[str, "MCPServer"], cwd: str
    ) -> Dict[str, "MCPServer"]:
        """Accepted declared sources as ``url`` entries; ``source_states`` says
        why each other one is left out. A failed read merges none."""
        from core import sources as declared

        try:
            row = self._declared_sources() or {}
        except Exception as exc:  # noqa: BLE001 - a source never stops the catalog
            logger.warning("Declared sources not read: %s", exc)
            row = {}
        merged: Dict[str, MCPServer] = {}
        states: Dict[str, str] = {}
        # Checked again here as at parse: a later release may add a catalog
        # server under a source's name, and the catalog entry wins.
        reserved = {"vigil": "Vigil's own server"}
        reserved.update({n: "a catalogue server" for n in catalog})
        # Merged instances are in ``catalog`` too; name them as what they are.
        reserved.update({n: "an integration instance" for n in self._instances})
        for name, source in sorted((row.get("sources") or {}).items()):
            if name in catalog:
                states[name] = (
                    f"not connected: the catalogue server {name!r} has this "
                    "name and wins"
                )
                continue
            if not declared.is_accepted(name, row):
                change = declared.pending_change(name, row) or {}
                what = ", ".join(change.get("changes") or ["its launch"])
                states[name] = (
                    "not connected: a new source waits for a person to accept it"
                    if change.get("kind") == "new"
                    else f"not connected: a change to {what} waits for a person "
                    "to accept it"
                )
                continue
            fields = list(source.get("credentials") or [])
            headers = dict(source.get("headers") or {})
            refused = (
                declared.name_refusal(name, reserved)
                or declared._check_url(source.get("url"))
                or next(
                    (
                        r
                        for h, v in headers.items()
                        if (r := declared._check_header(h, v, fields))
                    ),
                    None,
                )
            )
            if refused:
                states[name] = f"not connected: refused at merge: {refused}"
                continue
            used = {f for v in headers.values() for f in declared.TEMPLATE.findall(v)}
            merged[name] = MCPServer(
                name=name,
                command="",
                args=[],
                cwd=cwd,
                env={},
                required_env_vars=[
                    declared.credential_key(name, f) for f in sorted(used)
                ],
                url=source["url"],
                headers=headers,
                source=True,
            )
        self.source_states = states
        MCPService._sources_on = frozenset(merged)
        return merged

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

    def list_servers(self) -> List[str]:
        """
        List all available servers.

        Returns:
            List of server names.
        """
        return list(self.servers.keys())


def _applied_sources() -> Dict[str, Any]:
    from core.sources import applied_sources

    return applied_sources()
