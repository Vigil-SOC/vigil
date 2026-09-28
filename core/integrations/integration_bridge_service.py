"""Service to bridge frontend integration configs to MCP servers."""

import logging
import os
from typing import Dict, Optional, Tuple

from core.config import load_integrations_config
from core.integrations._base.descriptor import get_descriptor, iter_descriptors

logger = logging.getLogger(__name__)


# Values this module last wrote to each `<ID>_MCP_URL`. Lets us re-derive when a
# connectorUrl changes while still letting an operator-set env var win — keyed
# globally because os.environ (the thing we guard) is process-wide.
_DERIVED_MCP_URLS: Dict[str, str] = {}


# Form-field names contributed by the shared proxy block (see
# ``clients/web/src/config/integrations.ts``). These are meta-config — the
# bridge translates them into HTTPS_PROXY / ALL_PROXY env vars (which
# requests/httpx/urllib all honor) rather than emitting raw
# ``<ID>_PROXY_*`` vars that downstream MCP servers wouldn't know to
# read. PgBouncer / SSH-tunnel rewriting of the integration's host:port
# also happens here.
_PROXY_FORM_FIELDS = frozenset(
    {
        "proxy_type",
        "proxy_host",
        "proxy_port",
        "proxy_username",
        "proxy_password",
        "ssh_private_key_path",
        "ssh_key_passphrase",
        "verify_proxy_tls",
    }
)


class IntegrationBridgeService:
    """Bridges integration configs to MCP server configurations."""

    @staticmethod
    def server_names_for(integration_id: str) -> Tuple[str, ...]:
        """MCP server names backing an integration, straight from its descriptor.

        Previously a hand-written map whose every literal carried an invented
        ``-server`` suffix that matched no real ``mcp-config.json`` key.
        """
        descriptor = get_descriptor(integration_id)
        return descriptor.mcp_server_names if descriptor else ()

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
            env_key = f"{integration_id.upper().replace('-', '_')}_MCP_URL"
            current = os.environ.get(env_key)  # noqa: ENV001 - MCP child env
            if current and current != _DERIVED_MCP_URLS.get(env_key):
                continue  # operator-set (or externally changed) value wins
            # Trailing slash: the connector mounts streamable-HTTP at /mcp/, and
            # /mcp (no slash) 307-redirects — which mcp-remote won't follow.
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

    def _config_to_env_vars(self, integration_id: str, config: Dict) -> Dict[str, str]:
        """
        Convert integration configuration to environment variables.

        Args:
            integration_id: Integration identifier (e.g., 'virustotal')
            config: Integration configuration dictionary

        Returns:
            Dictionary of environment variables with proper naming
        """
        env_vars = {}

        # Add integration ID prefix for namespacing
        # Convert kebab-case to UPPER_SNAKE_CASE
        prefix = integration_id.upper().replace("-", "_")

        for field_name, field_value in config.items():
            # Proxy fields are translated separately below into
            # HTTPS_PROXY / ALL_PROXY (and host/port rewrites), not
            # exposed as <PREFIX>_PROXY_* — downstream MCP servers
            # already speak the conventional env-var names.
            if field_name in _PROXY_FORM_FIELDS:
                continue

            # Skip empty values
            if field_value is None or field_value == "":
                continue

            # Convert boolean to string
            if isinstance(field_value, bool):
                field_value = "true" if field_value else "false"

            # Convert field name to env var name
            env_name = field_name.upper()

            # Add prefix and set value
            full_env_name = f"{prefix}_{env_name}"
            env_vars[full_env_name] = str(field_value)

        # Append proxy-derived env vars (HTTPS_PROXY etc.) when this
        # integration's config asks for HTTP/SOCKS routing.
        env_vars.update(self._proxy_env_vars(integration_id, config))

        return env_vars

    def _proxy_env_vars(self, integration_id: str, config: Dict) -> Dict[str, str]:
        """Translate proxy_* form fields into env vars for the MCP child.

        For ``http`` / ``socks5``: emits ``HTTPS_PROXY`` / ``ALL_PROXY``
        (recognised by every common Python HTTP client).

        For ``pgbouncer`` and ``ssh_tunnel``: returns ``{}`` here. Those
        modes need host:port rewriting on URL-shaped fields, which is
        more invasive than env injection — handled (or warned about) by
        callers that actually open connections. Documented in the
        accompanying plan as a v1 limitation for integration-side
        tunneling.
        """
        proxy_type = (config.get("proxy_type") or "none").strip().lower()
        if proxy_type in ("", "none"):
            return {}

        try:
            from core.integrations.integration_secrets import secret_fields_for
            from core.storage.db_proxy import ProxyConfig, child_env_for_proxy
        except ImportError:  # pragma: no cover - defensive
            return {}

        secret_keys = secret_fields_for(integration_id)
        proxy = ProxyConfig.from_dict(
            config,
            password_secret_key=secret_keys.get("proxy_password"),
            passphrase_secret_key=secret_keys.get("ssh_key_passphrase"),
        )
        if proxy_type in ("http", "socks5"):
            return child_env_for_proxy(proxy)

        # pgbouncer / ssh_tunnel: log so operators know the bridge
        # itself doesn't handle endpoint rewriting for MCP children.
        logger.info(
            "Integration %s configured proxy_type=%s; bridge does not "
            "rewrite MCP server endpoints. Use http/socks5 for HTTP "
            "integrations, or configure the proxy at the network layer.",
            integration_id,
            proxy_type,
        )
        return {}

    def is_integration_enabled(self, integration_id: str) -> bool:
        """
        Check if an integration is enabled.

        Args:
            integration_id: Integration identifier

        Returns:
            True if integration is enabled, False otherwise
        """
        config = self.load_integration_config()
        return integration_id in config.get("enabled_integrations", [])

    def get_integration_config(self, integration_id: str) -> Optional[Dict]:
        """
        Get configuration for a specific integration.

        Args:
            integration_id: Integration identifier

        Returns:
            Integration configuration dictionary or None
        """
        config = self.load_integration_config()
        return config.get("integrations", {}).get(integration_id)

    def get_integration_status(self, integration_id: str) -> Dict:
        """
        Get status information for an integration.

        Args:
            integration_id: Integration identifier

        Returns:
            Dictionary with status information
        """
        config = self.load_integration_config()

        is_enabled = integration_id in config.get("enabled_integrations", [])
        has_config = integration_id in config.get("integrations", {})
        server_names = self.server_names_for(integration_id)
        has_server = bool(server_names)

        status = {
            "enabled": is_enabled,
            "configured": has_config,
            "server_available": has_server,
            "ready": is_enabled and has_config and has_server,
        }

        if has_server:
            status["server_names"] = list(server_names)

        return status

    def get_all_integration_statuses(self) -> Dict[str, Dict]:
        """
        Get status information for all integrations.

        Returns:
            Dictionary mapping integration IDs to their status
        """
        config = self.load_integration_config()
        all_integrations = {d.id for d in iter_descriptors() if d.mcp_server_names}
        all_integrations.update(config.get("integrations", {}).keys())

        statuses = {}
        for integration_id in all_integrations:
            statuses[integration_id] = self.get_integration_status(integration_id)

        return statuses
