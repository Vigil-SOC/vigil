"""Per-integration registry of secret-typed configuration fields.

Vigil's persistence story for integration credentials is split:

- **Non-secret config** (URLs, regions, verify_ssl flags, paths) goes into the
  ``IntegrationConfig`` database table via ``database.config_service`` and is
  mirrored to ``~/.deeptempo/integrations_config.json`` for back-compat.
- **Secret credentials** (API keys, passwords, bearer tokens) go into the
  encrypted secrets store at ``~/.vigil/secrets.enc`` via
  ``backend.secrets_manager.set_secret`` / ``get_secret``.

This module exposes the mapping from frontend form-field name → environment
variable name (which is also the secrets-store key) for each integration's
secret-typed fields. The generic ``POST /config/integrations`` save handler
uses it to:

1. Route the value of each registered secret field through ``set_secret`` so
   the credential lands in the encrypted store (and ``os.environ`` for the
   in-process backend, see ``SecretsManager.set``).
2. Strip the field from the dict that gets persisted to the DB / JSON, so we
   never write plaintext credentials to those stores.
3. On read, redact the same fields from the response so secrets don't leak
   back to the frontend.

When you add a new integration that has password-typed fields in
``frontend/src/config/integrations.ts``, add a tuple entry to
``_SECRET_FIELDS`` below. The default ``<INTEGRATION_ID>_<FIELD>``
convention is built automatically; add an ``_ENV_VAR_OVERRIDES`` entry
only when the consumer reads the secret under a non-canonical name
(e.g. CrowdStrike's official MCP server reads ``FALCON_*``).
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping

# Default form-field → env-var-suffix translations. Mirrors
# ``services.integration_bridge_service.IntegrationBridgeService.FIELD_TO_ENV_MAP``
# so credentials saved via the Settings UI land under the same env-var
# names that the bridge service uses when injecting env vars into MCP
# server child processes.
_DEFAULT_FIELD_SUFFIX: Mapping[str, str] = {
    "api_key": "API_KEY",
    "api_token": "API_TOKEN",
    "api_secret": "API_SECRET",
    "access_key": "ACCESS_KEY",
    "secret_key": "SECRET_KEY",
    "client_id": "CLIENT_ID",
    "client_secret": "CLIENT_SECRET",
    "username": "USERNAME",
    "password": "PASSWORD",
    "token": "TOKEN",
    "bot_token": "BOT_TOKEN",
    "auth_token": "AUTH_TOKEN",
    "webhook_url": "WEBHOOK_URL",
    "integration_key": "INTEGRATION_KEY",
    "credentials_json": "CREDENTIALS_JSON",
    "secret_access_key": "SECRET_ACCESS_KEY",
    "smtp_password": "SMTP_PASSWORD",
    "sec_token": "SEC_TOKEN",
    "api_key_secret": "API_KEY_SECRET",
    "inbound_api_key": "INBOUND_API_KEY",
}


def _default_env_var(integration_id: str, field_name: str) -> str:
    """Build the canonical env-var name for a given integration + field.

    Convention: ``<UPPER_SNAKE_INTEGRATION_ID>_<FIELD_SUFFIX>`` where the
    suffix comes from ``_DEFAULT_FIELD_SUFFIX`` if known, otherwise the
    upper-snake-cased field name. Matches ``IntegrationBridgeService``'s
    convention for env-var injection into MCP server child processes.
    """
    prefix = integration_id.upper().replace("-", "_")
    suffix = _DEFAULT_FIELD_SUFFIX.get(field_name, field_name.upper())
    return f"{prefix}_{suffix}"


# Form-field names per integration that are sensitive (mirrors `type:
# 'password'` entries in ``frontend/src/config/integrations.ts``). The
# values get routed through the secrets manager rather than persisted
# plaintext to the DB / JSON file.
_SECRET_FIELDS: Mapping[str, tuple[str, ...]] = {
    "github": ("token",),
    "virustotal": ("api_key",),
    "alienvault-otx": ("api_key",),
    "shodan": ("api_key",),
    "misp": ("api_key",),
    "gcp-threat-intel": ("api_key",),
    "url-analysis": ("api_key",),
    "ip-geolocation": ("api_key",),
    "crowdstrike": ("client_secret",),
    "sentinelone": ("api_token",),
    "carbon-black": ("api_key",),
    "microsoft-defender": ("client_secret",),
    "cortex-xdr": ("api_key",),
    "trend-micro-vision-one": ("api_token",),
    "sophos-intercept-x": ("client_secret",),
    "cybereason": ("password",),
    "trellix": ("client_secret", "api_key"),
    "tanium": ("password",),
    "cynet": ("api_key",),
    "eset": ("password",),
    "bitdefender-gravityzone": ("api_key",),
    "fortinet-fortiedr": ("api_token",),
    "kaspersky": ("password",),
    "cisco-secure-endpoint": ("api_key",),
    "symantec-edr": ("client_secret",),
    "splunk": ("password",),
    "cribl-stream": ("password",),
    "elastic-siem": ("api_key", "password"),
    "azure-sentinel": ("client_secret",),
    "qradar": ("sec_token",),
    "arcsight": ("password",),
    "logrhythm": ("api_token",),
    "exabeam": ("password",),
    "securonix": ("password",),
    "sumo-logic": ("access_key",),
    "graylog": ("api_token",),
    "aws-security-hub": ("secret_access_key",),
    "aws-guardduty": ("secret_access_key",),
    "gcp-security": ("credentials_json",),
    "azure-defender": ("client_secret",),
    "prisma-cloud": ("secret_key",),
    "orca-security": ("api_token",),
    "wiz": ("client_secret",),
    "lacework": ("api_secret",),
    "aqua-security": ("password",),
    "snyk": ("api_token",),
    "okta": ("api_token",),
    "azure-ad": ("client_secret",),
    "ping-identity": ("client_secret",),
    "auth0": ("client_secret",),
    "onelogin": ("client_secret",),
    "duo-security": ("secret_key",),
    "jumpcloud": ("api_key",),
    "sailpoint": ("client_secret",),
    "cyberark": ("password",),
    "beyond-trust": ("api_key",),
    "palo-alto": ("api_key",),
    "cisco-firepower": ("password",),
    "fortinet": ("api_key",),
    "checkpoint": ("password",),
    "zscaler": ("api_key", "password"),
    "sophos": ("api_token",),
    "cloudflare": ("api_token",),
    "cloudforce_one": ("api_token",),
    "juniper-srx": ("password",),
    "jira": ("api_token",),
    "servicenow": ("password",),
    "thehive": ("api_key",),
    "cortex-xsoar": ("api_key",),
    "swimlane": ("password",),
    "ibm-resilient": ("api_key_secret",),
    "opsgenie": ("api_key",),
    "slack": ("bot_token",),
    "pagerduty": ("api_token", "integration_key"),
    "microsoft-teams": ("webhook_url",),
    "email": ("smtp_password",),
    "webhook": ("auth_token",),
    "discord": ("webhook_url",),
    "mattermost": ("webhook_url",),
    "hybrid-analysis": ("api_key",),
    "joe-sandbox": ("api_key",),
    "anyrun": ("api_key",),
    "timesketch": ("password", "api_token"),
    "velociraptor": ("api_key",),
    "grr": ("password",),
    "autopsy": ("password",),
    "osquery": ("api_token",),
    "cuckoo": ("api_token",),
    "vstrike": ("api_key", "inbound_api_key", "username", "password"),
}


# Per-integration overrides where the consumer reads the secret under a
# name that doesn't match the default ``<ID>_<FIELD>`` convention.
# Anything NOT listed here uses ``_default_env_var(integration_id, field)``.
#
# Each entry is keyed by integration_id; values are partial maps from
# form-field name → env-var name. Missing fields fall back to the default.
_ENV_VAR_OVERRIDES: Mapping[str, Mapping[str, str]] = {
    # CrowdStrike's official MCP server (falcon-mcp) reads FALCON_*
    # rather than CROWDSTRIKE_*. Match the upstream so secrets saved
    # via the Settings UI flow straight into the MCP server.
    "crowdstrike": {"client_secret": "FALCON_CLIENT_SECRET"},
    # mcp-config.json's PagerDuty server reads ${PAGERDUTY_API_KEY},
    # not PAGERDUTY_API_TOKEN.
    "pagerduty": {"api_token": "PAGERDUTY_API_KEY"},
}


def _resolve_env_var(integration_id: str, field_name: str) -> str:
    """Resolve the secrets-store key for one integration field."""
    overrides = _ENV_VAR_OVERRIDES.get(integration_id, {})
    return overrides.get(field_name) or _default_env_var(integration_id, field_name)


def _build_registry() -> Dict[str, Dict[str, str]]:
    """Materialize the per-integration secret registry from the field list."""
    return {
        integration_id: {
            field: _resolve_env_var(integration_id, field) for field in fields
        }
        for integration_id, fields in _SECRET_FIELDS.items()
    }


# integration_id → {form_field_name: secrets_manager_key}
INTEGRATION_SECRET_FIELDS: Mapping[str, Mapping[str, str]] = _build_registry()


def secret_fields_for(integration_id: str) -> Mapping[str, str]:
    """Return the secret-field map for an integration, empty if unregistered."""
    return INTEGRATION_SECRET_FIELDS.get(integration_id, {})


def split_secrets(
    integration_id: str, config: Dict[str, object]
) -> tuple[Dict[str, str], Dict[str, object]]:
    """Partition a config dict into (secrets, non_secrets).

    `secrets` maps secrets-store key → value (ready to feed `set_secret`).
    Empty-string and `None` values are kept in `secrets` so the caller can
    decide whether to apply or skip them (the convention is "empty means
    don't overwrite an existing secret").

    The returned non_secrets dict is a fresh copy with secret fields
    removed — safe to persist to the DB / JSON.
    """
    mapping = secret_fields_for(integration_id)
    if not mapping:
        return {}, dict(config)

    secrets: Dict[str, str] = {}
    non_secrets: Dict[str, object] = {}
    for field, value in config.items():
        env_key = mapping.get(field)
        if env_key is None:
            non_secrets[field] = value
            continue
        # Coerce to string so callers don't have to. Non-string values for
        # secret fields are pathological — log via the redact step if needed.
        secrets[env_key] = "" if value is None else str(value)
    return secrets, non_secrets


def redact_secrets(integration_id: str, config: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of ``config`` with registered secret fields removed.

    Used by the GET handler so the frontend never receives plaintext
    credentials. The form will treat absent secret fields as "leave existing
    value untouched" on the next save.
    """
    mapping = secret_fields_for(integration_id)
    if not mapping:
        return dict(config)
    return {k: v for k, v in config.items() if k not in mapping}


def secret_field_names(integration_id: str) -> Iterable[str]:
    """Iterable over the form-field names that are secrets for an integration."""
    return secret_fields_for(integration_id).keys()
