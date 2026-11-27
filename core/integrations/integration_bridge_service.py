"""Service to bridge frontend integration configs to MCP servers."""

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional, Tuple

from core.config import REPO_ROOT, load_integrations_config
from core.integrations._base.descriptor import get_descriptor
from core.integrations.extension.trust import is_trusted_connector_url

logger = logging.getLogger(__name__)


# Values this module last wrote to each `<ID>_MCP_URL`. Lets us re-derive when a
# connectorUrl changes while still letting an operator-set env var win — keyed
# globally because os.environ (the thing we guard) is process-wide.
_DERIVED_MCP_URLS: Dict[str, str] = {}


# The environment this process started with, before any save wrote to it.
_BOOT_ENV = frozenset(os.environ)  # noqa: ENV001 - names only, for collisions

# --- Instances (#5353) --------------------------------------------------------
# A second (third, ...) instance of a launched integration is an
# integration_configs row ``<integration-id>-<label>`` whose config holds
# ``instance_of``. The same id is its server name, secrets prefix and health key.

INSTANCE_OF = "instance_of"
LABEL = re.compile(r"[a-z][a-z0-9]{0,11}")
# The runner puts an instance's HOME at <base HOME>/<label>; the base's TMPDIR
# is <base HOME>/tmp.
RESERVED_LABELS = frozenset({"tmp"})


def valid_label(label: str) -> bool:
    return bool(LABEL.fullmatch(label)) and label not in RESERVED_LABELS


MAX_TOOL_NAME = 64
# falcon-mcp reads these names; the base launch's ${VAR}s include them.
FALCON_ENV = {"client_id": "FALCON_CLIENT_ID", "base_url": "FALCON_BASE_URL"}
# A ${VAR} or ${VAR:-default} name in an mcp-config.json value.
VAR_NAME = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?=[}:])")
# Placeholders that are path sentinels, not settings.
PATH_PLACEHOLDERS = frozenset({"workspaceFolder", "HOME", "PYTHONPATH", "VIGIL_DIR"})


@lru_cache(maxsize=1)
def catalog_entries() -> Dict[str, dict]:
    """mcp-config.json's raw entries, comment keys dropped. Read once: it ships
    with the image, so a change means a restart anyway."""
    try:
        raw = json.loads((REPO_ROOT / "mcp-config.json").read_text())["mcpServers"]
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("mcp-config.json not read for instances: %s", exc)
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def base_of(integration_id: str) -> Optional[str]:
    """The integration ``integration_id`` names an instance of, by its form alone.

    No database read, so the runner and a server's own process answer the same
    way. A descriptor id or catalogue server is never an instance (so
    ``splunk-selfhosted`` stays Splunk's own server).
    """
    from core.integrations._base.descriptor import get_descriptor

    base, sep, label = (integration_id or "").rpartition("-")
    if not sep or not valid_label(label) or get_descriptor(base) is None:
        return None
    if get_descriptor(integration_id) is not None:
        return None
    if integration_id in catalog_entries():
        return None
    return base


def instances(rows: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Each saved instance -> its base: rows whose ``instance_of`` matches their id."""
    if rows is None:
        rows = load_integrations_config()["integrations"]
    return {
        iid: cfg[INSTANCE_OF]
        for iid, cfg in sorted(rows.items())
        if isinstance(cfg, dict)
        and cfg.get(INSTANCE_OF)
        and base_of(iid) == cfg.get(INSTANCE_OF)
    }


def launch_fields(base: str) -> Dict[str, str]:
    """Each name the base launch may read a field under -> that field."""
    from core.integrations._base.descriptor import get_descriptor
    from core.integrations.integration_secrets import env_var_for

    descriptor = get_descriptor(base)
    if descriptor is None:
        return {}
    out = {env_var_for(base, f.name): f.name for f in descriptor.fields}
    if base == "crowdstrike":
        out.update({var: field for field, var in FALCON_ENV.items()})
    return out


def instance_keys(instance_id: str) -> Dict[str, str]:
    """An instance's field -> its own env/secret name: always ``<ID>_<FIELD>``."""
    from core.integrations._base.descriptor import get_descriptor
    from core.integrations.integration_secrets import default_env_var

    descriptor = get_descriptor(base_of(instance_id) or "")
    if descriptor is None:
        return {}
    return {f.name: default_env_var(instance_id, f.name) for f in descriptor.fields}


def instance_renames(instance_id: str) -> Dict[str, str]:
    """Base launch ``${VAR}`` -> the instance's own name for the same field."""
    keys = instance_keys(instance_id)
    return {
        var: keys[field]
        for var, field in launch_fields(base_of(instance_id) or "").items()
        if field in keys
    }


def instances_allowed(base: str) -> Tuple[bool, Optional[str]]:
    """Whether ``base`` offers a second instance, and why not when it doesn't."""
    from core.integrations._base.descriptor import get_descriptor

    if get_descriptor(base) is None:
        return False, f"{base} has no descriptor"
    entry = catalog_entries().get(base)
    if entry is None:
        return False, f"{base} has no catalogue server named {base}"
    if "url" in entry:
        return False, (
            f"{base} is reached at a URL, not launched: add another as a "
            "declared source"
        )
    values = [
        str(v) for k, v in (entry.get("env") or {}).items() if not k.startswith("_")
    ]
    values += [str(a) for a in entry.get("args") or []]
    known = launch_fields(base)
    unknown = sorted(
        {
            m.group(1)
            for v in values
            for m in VAR_NAME.finditer(v.replace("$$", ""))
            if m.group(1) not in known and m.group(1) not in PATH_PLACEHOLDERS
        }
    )
    if "HOME" in {m.group(1) for v in values for m in VAR_NAME.finditer(v)}:
        return False, (
            f"{base}'s launch reads files under its HOME, which an instance does "
            "not share"
        )
    if unknown:
        return False, (
            f"{base}'s launch reads {', '.join(unknown)}, which is not one of its "
            "settings, so a second instance could not be given its own"
        )
    return True, None


def instance_refusal(
    instance_id: str,
    base: Any,
    *,
    base_tools: Optional[Iterable[str]],
    taken: Iterable[str] = (),
    other_instances: Iterable[str] = (),
) -> Optional[str]:
    """Why ``instance_id`` cannot be created as an instance of ``base``, or None.

    ``base_tools`` is the base server's tool list as connected now (None when
    it is not connected); ``taken`` holds the declared source names.
    """
    from core.integrations._base.descriptor import get_descriptor, iter_descriptors
    from core.integrations.integration_secrets import (
        ENV_CREDENTIAL_NAMES,
        INTEGRATION_SECRET_FIELDS,
        env_var_for,
    )

    if not isinstance(base, str) or not base:
        return "instance_of must name the integration this is an instance of"
    allowed, reason = instances_allowed(base)
    if not allowed:
        return reason
    prefix = f"{base}-"
    label = instance_id[len(prefix) :] if instance_id.startswith(prefix) else ""
    if not valid_label(label):
        return (
            f"the id must be {prefix}<label>, the label 1 to 12 lowercase letters "
            "and digits starting with a letter, and not tmp"
        )
    if instance_id == "vigil" or get_descriptor(instance_id) is not None:
        return f"{instance_id} is taken: an integration has this id"
    if instance_id in catalog_entries():
        return f"{instance_id} is taken: a catalogue server has this name"
    if instance_id in set(taken):
        return f"{instance_id} is taken: a declared source has this name"
    if instance_id in INTEGRATION_SECRET_FIELDS:
        return f"{instance_id} is taken: an integration has this id"
    existing = set(ENV_CREDENTIAL_NAMES)
    for d in iter_descriptors():
        existing |= {env_var_for(d.id, f.name) for f in d.fields}
    for keys in INTEGRATION_SECRET_FIELDS.values():
        existing |= set(keys.values())
    for other in other_instances:
        if other != instance_id:
            existing |= set(instance_keys(other).values())
    existing |= set(FALCON_ENV.values())
    own = set(instance_keys(instance_id).values())
    # A name the deployment sets (e.g. WAZUH_SANDBOX_*) would hand the instance
    # a value it was never given. Not os.environ now: a save writes there too.
    clash = sorted(own & (existing | _BOOT_ENV))
    if clash:
        return f"its setting {clash[0]} would collide with one that exists"
    if base_tools is None:
        return (
            f"{base} is not connected: connect it first, so its tool names can be "
            f"checked against the {MAX_TOOL_NAME}-character limit"
        )
    long = [t for t in base_tools if len(f"{instance_id}_{t}") > MAX_TOOL_NAME]
    if long:
        return (
            f"{instance_id}_{long[0]} would be over {MAX_TOOL_NAME} characters; "
            "choose a shorter label"
        )
    return None


class IntegrationBridgeService:
    """Bridges integration configs to MCP server configurations."""

    @staticmethod
    def server_names_for(integration_id: str) -> Tuple[str, ...]:
        """MCP server names backing an integration, straight from its descriptor.

        Previously a hand-written map whose every literal carried an invented
        ``-server`` suffix that matched no real ``mcp-config.json`` key.
        """
        descriptor = get_descriptor(integration_id)
        if descriptor is None and base_of(integration_id):
            return (integration_id,)  # an instance's server is its id
        return descriptor.mcp_server_names if descriptor else ()

    @staticmethod
    def instances() -> Dict[str, str]:
        """Saved instances -> base; ``MCPService.load_catalog`` merges these."""
        return instances()

    def load_integration_config(self) -> Dict:
        """
        Load integration configuration.

        Same reader as ``core.config.get_integration_config``: database rows
        when present, the JSON file only when the table is empty or unreachable.

        Returns:
            Dictionary with 'enabled_integrations' and 'integrations' keys
        """
        state = load_integrations_config()
        enabled = state["enabled_integrations"]
        logger.info(
            "Loaded integration config with %d enabled integrations", len(enabled)
        )
        return {
            "enabled_integrations": enabled,
            "integrations": state["integrations"],
        }

    def derive_remote_mcp_env(self) -> Dict[str, str]:
        """Derive a ``<UPPER_ID>_MCP_URL`` env var from each configured
        integration's ``connectorUrl`` so a static ``mcp-config.json`` entry
        (e.g. ``${LOGLM_MCP_URL}``) resolves from one source of truth. An
        explicit pre-existing env value wins. Returns the mapping applied."""
        config = self.load_integration_config()
        integrations = config.get("integrations", {})
        applied: Dict[str, str] = {}
        # Any configured connector, not just enabled ones: the master toggle
        # enables the MCP server before adding the integration to
        # enabled_integrations, so gating on enablement raced to an empty URL.
        for integration_id, cfg in integrations.items():
            connector_url = (cfg or {}).get("connectorUrl")
            if not connector_url:
                continue
            # The value becomes an MCP child's argv, so hold it to the same
            # trust rule as the session mint, and keep placeholders out of it.
            if "${" in str(connector_url) or not is_trusted_connector_url(
                str(connector_url)
            ):
                logger.warning(
                    "Ignoring connectorUrl for '%s': not a trusted https origin",
                    integration_id,
                )
                continue
            env_key = f"{integration_id.upper().replace('-', '_')}_MCP_URL"
            current = os.environ.get(env_key)  # noqa: ENV001 - MCP child env
            if current and current != _DERIVED_MCP_URLS.get(env_key):
                continue  # operator-set (or externally changed) value wins
            # Trailing slash: the connector mounts streamable-HTTP at /mcp/, and
            # /mcp (no slash) 307-redirects; the slash saves every request that hop.
            value = str(connector_url).rstrip("/") + "/mcp/"
            _DERIVED_MCP_URLS[env_key] = value
            if current == value:
                continue  # unchanged; nothing to re-apply
            os.environ[env_key] = value  # noqa: ENV001 - MCP child env
            applied[env_key] = value
        if applied:
            logger.info(
                "Derived remote MCP URLs from integration configs: %s",
                list(applied),
            )
        return applied
