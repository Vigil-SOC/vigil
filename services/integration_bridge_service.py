"""Service to bridge frontend integration configs to MCP servers."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Form-field names contributed by the shared proxy block (see
# ``frontend/src/config/integrations.ts``). These are meta-config — the
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

    # Map integration IDs (frontend) to MCP server names (backend)
    INTEGRATION_TO_SERVER_MAP = {
        # Threat Intelligence
        "virustotal": "virustotal-server",
        "alienvault-otx": "alienvault-otx-server",
        "shodan": "shodan-server",
        "misp": "misp-server",
        "url-analysis": "url-analysis-server",
        "ip-geolocation": "ip-geolocation-server",
        # EDR/XDR
        "crowdstrike": "crowdstrike-server",
        "sentinelone": "sentinelone-server",
        "carbon-black": "carbon-black-server",
        "microsoft-defender": "microsoft-defender-server",
        # SIEM
        "splunk": "splunk-server",
        "azure-sentinel": "azure-sentinel-server",
        # Cloud Security
        "aws-security-hub": "aws-security-hub-server",
        "gcp-security": "gcp-security-server",
        # Identity & Access
        "okta": "okta-server",
        "azure-ad": "azure-ad-server",
        # Network Security
        "palo-alto": "palo-alto-server",
        # Incident Management
        "jira": "jira-server",
        # Communications
        "slack": "slack-server",
        "pagerduty": "pagerduty-server",
        "microsoft-teams": "microsoft-teams-server",
        # Sandbox Analysis
        "hybrid-analysis": "hybrid-analysis-server",
        "joe-sandbox": "joe-sandbox-server",
        "anyrun": "anyrun-server",
        "cape-sandbox": "cape-sandbox-server",
        "cape_sandbox": "cape-sandbox-server",
    }

    # Map integration field names to environment variable names
    # These are common patterns - specific integrations may need custom mappings
    FIELD_TO_ENV_MAP = {
        "api_key": "API_KEY",
        "api_token": "API_TOKEN",
        "api_secret": "API_SECRET",
        "access_key": "ACCESS_KEY",
        "secret_key": "SECRET_KEY",
        "client_id": "CLIENT_ID",
        "client_secret": "CLIENT_SECRET",
        "username": "USERNAME",
        "password": "PASSWORD",
        "server_url": "SERVER_URL",
        "api_url": "API_URL",
        "base_url": "BASE_URL",
        "url": "URL",
        "hostname": "HOSTNAME",
        "tenant_id": "TENANT_ID",
        "workspace_id": "WORKSPACE_ID",
        "region": "REGION",
        "domain": "DOMAIN",
        "org_key": "ORG_KEY",
        "organization_id": "ORGANIZATION_ID",
        "project_id": "PROJECT_ID",
        "verify_ssl": "VERIFY_SSL",
        "port": "PORT",
        "access_key_id": "ACCESS_KEY_ID",
        "secret_access_key": "SECRET_ACCESS_KEY",
        "credentials_json": "CREDENTIALS_JSON",
        "integration_key": "INTEGRATION_KEY",
        "webhook_url": "WEBHOOK_URL",
        "bot_token": "BOT_TOKEN",
        "default_channel": "DEFAULT_CHANNEL",
    }

    def __init__(self):
        """Initialize the integration bridge service."""
        self.config_path = Path.home() / ".deeptempo" / "integrations_config.json"

    def load_integration_config(self) -> Dict:
        """
        Load integration configuration from disk.

        Returns:
            Dictionary with 'enabled_integrations' and 'integrations' keys
        """
        if not self.config_path.exists():
            logger.info("No integration config file found, using empty config")
            return {"enabled_integrations": [], "integrations": {}}

        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)
            logger.info(
                f"Loaded integration config with {len(config.get('enabled_integrations', []))} enabled integrations"
            )
            return config
        except Exception as e:
            logger.error(f"Error loading integration config: {e}")
            return {"enabled_integrations": [], "integrations": {}}

    def get_enabled_servers(self) -> Dict[str, Dict]:
        """
        Get MCP server configurations for all enabled integrations.

        Returns:
            Dict mapping server names to their configurations with env vars
            Example: {
                'virustotal-server': {
                    'integration_id': 'virustotal',
                    'env_vars': {'VIRUSTOTAL_API_KEY': 'xxx'}
                }
            }
        """
        config = self.load_integration_config()
        enabled = config.get("enabled_integrations", [])
        integrations = config.get("integrations", {})

        servers = {}

        for integration_id in enabled:
            # Check if this integration has a corresponding MCP server
            server_name = self.INTEGRATION_TO_SERVER_MAP.get(integration_id)
            if not server_name:
                logger.debug(f"No MCP server mapped for integration: {integration_id}")
                continue

            # Get integration configuration
            integration_config = integrations.get(integration_id, {})
            if not integration_config:
                logger.warning(
                    f"No configuration found for enabled integration: {integration_id}"
                )
                continue

            # Convert integration config to environment variables
            env_vars = self._config_to_env_vars(integration_id, integration_config)

            servers[server_name] = {
                "integration_id": integration_id,
                "env_vars": env_vars,
                "config": integration_config,
            }

            logger.info(f"Prepared server config for {server_name} ({integration_id})")

        return servers

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
            env_name = self.FIELD_TO_ENV_MAP.get(field_name, field_name.upper())

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
            from services.db_proxy import ProxyConfig, child_env_for_proxy
            from services.integration_secrets import secret_fields_for
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
        has_server = integration_id in self.INTEGRATION_TO_SERVER_MAP

        status = {
            "enabled": is_enabled,
            "configured": has_config,
            "server_available": has_server,
            "ready": is_enabled and has_config and has_server,
        }

        if has_server:
            status["server_name"] = self.INTEGRATION_TO_SERVER_MAP[integration_id]

        return status

    def get_all_integration_statuses(self) -> Dict[str, Dict]:
        """
        Get status information for all integrations.

        Returns:
            Dictionary mapping integration IDs to their status
        """
        config = self.load_integration_config()
        all_integrations = set(self.INTEGRATION_TO_SERVER_MAP.keys())
        all_integrations.update(config.get("integrations", {}).keys())

        statuses = {}
        for integration_id in all_integrations:
            statuses[integration_id] = self.get_integration_status(integration_id)

        return statuses

    def get_server_module_path(self, integration_id: str) -> Optional[str]:
        """
        Get the Python module path for an integration's MCP server.

        Args:
            integration_id: Integration identifier

        Returns:
            Module path string or None
        """
        server_name = self.INTEGRATION_TO_SERVER_MAP.get(integration_id)
        if not server_name:
            return None

        # Convert server name to module name
        # Example: 'virustotal-server' -> 'virustotal'
        tool_name = server_name.replace("-server", "").replace("-", "_")

        return f"tools.{tool_name}"


# Global instance
_bridge_service = None


def get_integration_bridge() -> IntegrationBridgeService:
    """
    Get the global integration bridge service instance.

    Returns:
        IntegrationBridgeService instance
    """
    global _bridge_service
    if _bridge_service is None:
        _bridge_service = IntegrationBridgeService()
    return _bridge_service
