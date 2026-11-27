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
only when the consumer reads the value under a non-canonical name
(e.g. CrowdStrike's official MCP server reads ``FALCON_*``).
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Mapping

from core.config import vigil_path
from core.integrations._base.descriptor import iter_descriptors
from core.secrets import get_secret
from core.storage.config_service import register_integration_secret_redactor


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
    # mint_secret: bearer secret for minting session tokens
    # (core/integrations/extension/session_service.py).
    # mcp_token: static bearer the LogLM MCP tools present to the connector.
    "loglm": ("mint_secret", "mcp_token"),
    "gcp-threat-intel": ("api_key",),
    "firecrawl": ("api_key",),
    "cribl-stream": ("password",),
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
    # env.example and every existing deployment spell the self-hosted REST
    # endpoint SPLUNK_URL, not the canonical SPLUNK_SERVER_URL. The resolver's
    # env fallback for server_url keeps that name so an env-only deployment with
    # nothing saved in Settings still constructs the client.
    "splunk": {"server_url": "SPLUNK_URL"},
    # env.example, the Helm chart and mcp-config.json spell the VStrike
    # endpoint VSTRIKE_BASE_URL, not the canonical VSTRIKE_URL.
    "vstrike": {"url": "VSTRIKE_BASE_URL"},
}


# Form-field names contributed by the shared proxy block (see
# ``clients/web/src/config/integrationSchema.ts:PROXY_FIELDS``). Integrations
# that opt in via ``PROXY_SUPPORTED`` get these added to their
# secret-field registry so credentials are routed to the encrypted
# store rather than persisted plaintext on the integration row.
_PROXY_SECRET_FIELDS: tuple[str, ...] = ("proxy_password",)


# Integrations whose UI form includes the shared proxy field block.
# Kept as a frozen set so the registry stays greppable: adding an
# integration here means proxy_password gets the same encrypted-store
# treatment as the integration's own credentials. Each must also pass
# ``integration_proxy`` to its httpx clients (``_base/proxy.py``).
PROXY_SUPPORTED: frozenset[str] = frozenset(
    {
        "splunk",
        "elastic-siem",
        "opensearch",
        "misp",
        "wazuh",
        "opencti",
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


# Credentials env.example documents that are not integration form fields: the
# encrypted store owns them, read via get_secret so a value saved in the UI wins
# over the environment. Usernames and client IDs ride along; over-redacting them
# in a support bundle is harmless. Feeds scripts/vigil-support/secret-names.txt.
# Settings fields whose values are secrets. Not part of ENV_CREDENTIAL_NAMES:
# that set is "documented in env.example and not a Settings field".
SETTINGS_SECRET_NAMES: frozenset[str] = frozenset({"BIFROST_ADMIN_PASSWORD"})


ENV_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {
        "AGENT_INTERNAL_TOKEN",
        "AGENT_INTERNAL_TOKEN_PREVIOUS",
        "ALIENVAULT_OTX_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "CAPE_SANDBOX_API_KEY",
        "CLOUDFORCE_ONE_API_TOKEN",
        "CLOUDY_WEBHOOK_SECRET",
        "CRIBL_PASSWORD",
        "CRIBL_USERNAME",
        "CROWDSTRIKE_CLIENT_ID",
        "CROWDSTRIKE_CLIENT_SECRET",
        "DAEMON_WEBHOOK_TOKEN",
        "DARKTRACE_WEBHOOK_SECRET",
        "ELASTIC_SIEM_API_KEY",
        "ELASTIC_SIEM_PASSWORD",
        "ELASTIC_SIEM_USERNAME",
        "OPENSEARCH_PASSWORD",
        "OPENSEARCH_USERNAME",
        "GITHUB_TOKEN",
        "JOE_SANDBOX_API_KEY",
        "JWT_SECRET_KEY",
        "KAFKA_SASL_PASSWORD",
        "KAFKA_SASL_USERNAME",
        "MCP_RUNNER_TOKEN",
        "METRICS_TOKEN",
        "OPENAI_API_KEY",
        "PAGERDUTY_ROUTING_KEY",
        # vigil_app's password. Init, Compose and Helm read this name. App
        # processes receive the value as POSTGRES_PASSWORD, so Python does not.
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_PASSWORD",
        "SHELL_TOKEN",
        "SHODAN_API_KEY",
        "SLACK_BOT_TOKEN",
        "SMTP_PASSWORD",
        "SPLUNK_PASSWORD",
        "SPLUNK_USERNAME",
        "TEAMS_WEBHOOK_URL",
        "VIRUSTOTAL_API_KEY",
        "VSTRIKE_API_KEY",
        "VSTRIKE_INBOUND_API_KEY",
        "VSTRIKE_PASSWORD",
        "VSTRIKE_USERNAME",
    }
)


# A Custom Integration's secret is stored under ``<ID>_<FIELD>``, so both are
# caller-chosen. Only a ``custom-`` id with a plain field name may hold one: its
# key then starts ``CUSTOM_`` and cannot name a platform secret (JWT_SECRET_KEY,
# AGENT_INTERNAL_TOKEN, a built-in integration's credential, ...).
CUSTOM_INTEGRATION_PREFIX = "custom-"
_CUSTOM_FIELD_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def custom_secret_allowed(integration_id: str, field_name: object) -> bool:
    """Whether a Custom Integration field may be stored as a secret."""
    return (
        integration_id.startswith(CUSTOM_INTEGRATION_PREFIX)
        and integration_id not in INTEGRATION_SECRET_FIELDS
        and isinstance(field_name, str)
        and bool(_CUSTOM_FIELD_RE.match(field_name))
    )


def _custom_password_fields(integration_id: str) -> List[str]:
    """Password-typed field names of a Custom Integration, from its saved metadata.

    Custom Integrations are defined at runtime, so they are read here rather than
    built into the registry at import. An unreadable file reads as no fields.
    """
    if integration_id in INTEGRATION_SECRET_FIELDS:
        return []
    try:
        metadata = json.loads(
            vigil_path("custom_integrations", "metadata.json").read_text()
        )
    except (OSError, ValueError):
        return []
    entry = metadata.get(integration_id) if isinstance(metadata, dict) else None
    fields = entry.get("fields") if isinstance(entry, dict) else None
    return [
        field["name"]
        for field in (fields if isinstance(fields, list) else [])
        if isinstance(field, dict)
        and field.get("type") == "password"
        and isinstance(field.get("name"), str)
    ]


def _custom_secret_fields(integration_id: str) -> Dict[str, str]:
    """Password-typed fields of a Custom Integration that may be stored."""
    return {
        name: env_var_for(integration_id, name)
        for name in _custom_password_fields(integration_id)
        if custom_secret_allowed(integration_id, name)
    }


def unstorable_secret_fields(integration_id: str) -> List[str]:
    """Password-typed Custom Integration fields whose store key is refused.

    Metadata saved before the ``custom-`` rule (or written by hand) can still
    declare them; their values are neither stored as secrets nor kept in plaintext.
    """
    return [
        name
        for name in _custom_password_fields(integration_id)
        if not custom_secret_allowed(integration_id, name)
    ]


def secret_fields_for(integration_id: str) -> Mapping[str, str]:
    """Return the secret-field map for an integration, empty if unregistered."""
    if integration_id.startswith("source-"):
        # A declared source's own fields only (#5302), from the applied row.
        from core.sources import source_secret_fields

        return source_secret_fields(integration_id)
    from core.integrations.integration_bridge_service import base_of

    if base := base_of(integration_id):
        # An instance (#5353): the base's secret fields under <ID>_<FIELD>,
        # never an _ENV_VAR_OVERRIDES name, which would collide with the base's.
        return {
            field: default_env_var(integration_id, field) for field in _fields_for(base)
        }
    return INTEGRATION_SECRET_FIELDS.get(integration_id) or _custom_secret_fields(
        integration_id
    )


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
    refused = set(unstorable_secret_fields(integration_id))
    if not mapping and not refused:
        return {}, dict(config)

    secrets: Dict[str, str] = {}
    non_secrets: Dict[str, object] = {}
    for field, value in config.items():
        if field in refused:
            continue  # never persisted in plaintext; the save route refuses a value
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
    hidden = {
        *secret_fields_for(integration_id),
        *unstorable_secret_fields(integration_id),
    }
    return {k: v for k, v in config.items() if k not in hidden}


# Storage records integration changes but cannot import this module.
register_integration_secret_redactor(redact_secrets)


# Config keys that say where the integration connects (server_url, connectorUrl,
# base_url, host, tenant, region, ...).
_DESTINATION_KEY = re.compile(
    r"url|uri|host|endpoint|server|domain|address|instance|tenant|region|(?:^|_)port$",
    re.IGNORECASE,
)


def _same_destination(old: object, new: object) -> bool:
    def norm(value: object) -> str:
        return "" if value is None else str(value).strip().rstrip("/")

    return norm(old) == norm(new)


def _verifies(value: object) -> bool:
    # Unset reads as "verify" in every integration; anything else not plainly
    # true is treated as off, the way the clients would read it.
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _trust_widened(
    stored_config: Mapping[str, object], new_config: Mapping[str, object]
) -> bool:
    """Whether the save loosens which server the stored credential will trust.

    Turning ``verify_ssl`` off, or any change to ``ca_cert_path`` (a blank one
    trusts the whole default store) or ``proxy_type``, lets a different server
    receive the credential. The save replaces the stored config, so an omitted
    key is a cleared one.
    """
    if _verifies(stored_config.get("verify_ssl")) and not _verifies(
        new_config.get("verify_ssl")
    ):
        return True
    if not _same_destination(
        stored_config.get("ca_cert_path"), new_config.get("ca_cert_path")
    ):
        return True

    def proxy(config: Mapping[str, object]) -> str:
        value = str(config.get("proxy_type") or "").strip().lower()
        return "" if value == "none" else value  # the form's default

    return proxy(stored_config) != proxy(new_config)


def credentials_to_resupply(
    integration_id: str,
    stored_config: Mapping[str, object],
    new_config: Mapping[str, object],
) -> List[str]:
    """Secret fields the caller must send again because the destination moved.

    A stored credential follows the saved destination on the next call, so
    changing where an integration connects must not carry the old credential
    to the new host. When a destination field differs from the stored one,
    every secret that is currently set has to be present and non-empty in
    ``new_config``; the names of those that are not are returned. Loosening
    TLS trust (see ``_trust_widened``) counts as a move.
    """
    moved = _trust_widened(stored_config, new_config) or any(
        _DESTINATION_KEY.search(key)
        and not _same_destination(stored_config.get(key), value)
        for key, value in new_config.items()
    )
    if not moved:
        return []
    return [
        field
        for field, env_key in secret_fields_for(integration_id).items()
        if get_secret(env_key) and not new_config.get(field)
    ]
