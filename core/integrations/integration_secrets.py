"""Per-integration registry of secret-typed configuration fields.

Vigil's persistence story for integration credentials is split:

- **Non-secret config** (URLs, regions, verify_ssl flags, paths) goes into the
  ``IntegrationConfig`` database table via ``core.storage.config_service`` and is
  mirrored to ``~/.vigil/integrations_config.json`` for back-compat.
- **Secret credentials** (API keys, passwords, bearer tokens) go into the
  encrypted secrets store at ``~/.vigil/secrets.enc`` via
  ``core.secrets_manager.set_secret`` / ``get_secret``.

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
``clients/web/src/config/integrations.ts``, mark those fields ``secret=True``
on the vendor's descriptor; the map below derives itself from the descriptors,
and only a Catalog Entry with no code behind it is listed literally in
``_CATALOG_ONLY_SECRET_FIELDS``. The default ``<INTEGRATION_ID>_<FIELD>``
convention is built automatically; add an ``_ENV_VAR_OVERRIDES`` entry
only when the consumer reads the secret under a non-canonical name
(e.g. CrowdStrike's official MCP server reads ``FALCON_*``).
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping

from core.integrations._base.descriptor import iter_descriptors


def default_env_var(integration_id: str, field_name: str) -> str:
    """Build the canonical env-var name for a given integration + field.

    Convention: ``<UPPER_SNAKE_INTEGRATION_ID>_<UPPER_FIELD_NAME>``. Matches
    ``IntegrationBridgeService``'s convention for env-var injection into MCP
    server child processes — the two lookup tables that used to state this
    were pure identity maps, so the convention alone is the rule.
    """
    prefix = integration_id.upper().replace("-", "_")
    suffix = field_name.upper()
    return f"{prefix}_{suffix}"


# Form-field names per integration that are sensitive (mirrors `type:
# 'password'` entries in ``clients/web/src/config/integrations.ts``). The
# values get routed through the secrets manager rather than persisted
# plaintext to the DB / JSON file.
_CATALOG_ONLY_SECRET_FIELDS: Mapping[str, tuple[str, ...]] = {
    "github": ("token",),
    # mint_secret: HMAC for minting session tokens (services/api/routers/extensions.py).
    # mcp_token: static bearer the LogLM MCP tools present to the connector.
    "loglm": ("mint_secret", "mcp_token"),
    "gcp-threat-intel": ("api_key",),
    "firecrawl": ("api_key",),
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
    "cribl-stream": ("password",),
    "qradar": ("sec_token",),
    "arcsight": ("password",),
    "logrhythm": ("api_token",),
    "exabeam": ("password",),
    "securonix": ("password",),
    "sumo-logic": ("access_key",),
    "graylog": ("api_token",),
    "aws-guardduty": ("secret_access_key",),
    "gcp-security": ("credentials_json",),
    "azure-defender": ("client_secret",),
    "prisma-cloud": ("secret_key",),
    "orca-security": ("api_token",),
    "wiz": ("client_secret",),
    "lacework": ("api_secret",),
    "aqua-security": ("password",),
    "snyk": ("api_token",),
    "ping-identity": ("client_secret",),
    "auth0": ("client_secret",),
    "onelogin": ("client_secret",),
    "duo-security": ("secret_key",),
    "jumpcloud": ("api_key",),
    "sailpoint": ("client_secret",),
    "cyberark": ("password",),
    "beyond-trust": ("api_key",),
    "cisco-firepower": ("password",),
    "fortinet": ("api_key",),
    "checkpoint": ("password",),
    "zscaler": ("api_key", "password"),
    "sophos": ("api_token",),
    "cloudforce_one": ("api_token",),
    "juniper-srx": ("password",),
    "servicenow": ("password",),
    "thehive": ("api_key",),
    "cortex-xsoar": ("api_key",),
    "swimlane": ("password",),
    "ibm-resilient": ("api_key_secret",),
    "opsgenie": ("api_key",),
    "email": ("smtp_password",),
    "webhook": ("auth_token",),
    "discord": ("webhook_url",),
    "mattermost": ("webhook_url",),
    "timesketch": ("password", "api_token"),
    "velociraptor": ("api_key",),
    "grr": ("password",),
    "autopsy": ("password",),
    "osquery": ("api_token",),
    "cuckoo": ("api_token",),
}


# Per-integration overrides where the consumer reads the secret under a
# name that doesn't match the default ``<ID>_<FIELD>`` convention.
# Anything NOT listed here uses ``default_env_var(integration_id, field)``.
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


# Form-field names contributed by the shared proxy block (see
# ``clients/web/src/config/integrations.ts:PROXY_FIELDS``). Integrations
# that opt in via ``PROXY_SUPPORTED`` get these added to their
# secret-field registry so credentials are routed to the encrypted
# store rather than persisted plaintext on the integration row.
_PROXY_SECRET_FIELDS: tuple[str, ...] = ("proxy_password", "ssh_key_passphrase")


# Integrations whose UI form includes the shared proxy field block.
# Kept as a frozen set so the registry stays greppable: adding an
# integration here means proxy_password / ssh_key_passphrase get the
# same encrypted-store treatment as the integration's own credentials.
PROXY_SUPPORTED: frozenset[str] = frozenset(
    {
        "splunk",
        "elastic-siem",
        "qradar",
        "arcsight",
        "logrhythm",
        "exabeam",
        "securonix",
        "sumo-logic",
        "graylog",
        "cribl-stream",
        "misp",
    }
)


def env_var_for(integration_id: str, field_name: str) -> str:
    """Resolve the env-var name for one integration field, overrides included.

    The rule cannot depend on whether the field is a secret: the resolver reads
    non-secret fields out of the same env channel, so a field named here has
    one name everywhere or the two halves drift.
    """
    overrides = _ENV_VAR_OVERRIDES.get(integration_id, {})
    return overrides.get(field_name) or default_env_var(integration_id, field_name)


def _secret_fields() -> Dict[str, tuple[str, ...]]:
    """Every integration's secret fields: descriptors first, catalog-only after.

    A code-backed integration states its secret fields once, on its descriptor.
    Only Catalog Entries — a credential form with no Vigil code behind it — are
    listed literally above.
    """
    merged: Dict[str, tuple[str, ...]] = dict(_CATALOG_ONLY_SECRET_FIELDS)
    for descriptor in iter_descriptors():
        if descriptor.secret_fields:
            merged[descriptor.id] = descriptor.secret_fields
    return merged


def _fields_for(integration_id: str) -> Iterable[str]:
    """All secret-field names for an integration, including proxy fields
    contributed by the shared block when the integration opts in."""
    base = _secret_fields().get(integration_id, ())
    if integration_id in PROXY_SUPPORTED:
        return (*base, *_PROXY_SECRET_FIELDS)
    return base


def _build_registry() -> Dict[str, Dict[str, str]]:
    """Materialize the per-integration secret registry from the field list."""
    integration_ids = set(_secret_fields()) | PROXY_SUPPORTED
    return {
        integration_id: {
            field: env_var_for(integration_id, field)
            for field in _fields_for(integration_id)
        }
        for integration_id in integration_ids
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
