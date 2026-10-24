"""Service to bridge frontend integration configs to MCP servers."""

import logging
import os
from typing import Dict, Tuple

from core.config import load_integrations_config
from core.integrations._base.descriptor import get_descriptor
from core.integrations.extension.trust import is_trusted_connector_url

logger = logging.getLogger(__name__)


# Values this module last wrote to each `<ID>_MCP_URL`. Lets us re-derive when a
# connectorUrl changes while still letting an operator-set env var win — keyed
# globally because os.environ (the thing we guard) is process-wide.
_DERIVED_MCP_URLS: Dict[str, str] = {}


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
