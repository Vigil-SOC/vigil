"""Unit tests for the per-integration secret-field registry."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.integration_secrets import (  # noqa: E402
    INTEGRATION_SECRET_FIELDS,
    redact_secrets,
    secret_field_names,
    secret_fields_for,
    split_secrets,
)


def test_vstrike_secret_fields_registered():
    """VStrike's secret fields round-trip to env-var keys.

    JWT-only auth: only username + password are user-supplied secrets. The
    legacy api_key / inbound_api_key entries were retired in favor of
    Vigil minting/refreshing the token internally."""
    fields = secret_fields_for("vstrike")
    assert fields == {
        "username": "VSTRIKE_USERNAME",
        "password": "VSTRIKE_PASSWORD",
    }


def test_secret_fields_for_unregistered_returns_empty():
    assert secret_fields_for("not-a-real-integration") == {}


def test_split_secrets_partitions_correctly():
    raw = {
        "url": "https://vstrike.net",
        "verify_ssl": True,
        "username": "alice",
        "password": "wonderland",
    }
    secrets, non_secrets = split_secrets("vstrike", raw)
    # Secrets keyed by env-var name, ready for set_secret().
    assert secrets == {
        "VSTRIKE_USERNAME": "alice",
        "VSTRIKE_PASSWORD": "wonderland",
    }
    # Non-secrets retain original field names; no plaintext credentials.
    assert non_secrets == {
        "url": "https://vstrike.net",
        "verify_ssl": True,
    }


def test_split_secrets_keeps_empty_secret_values():
    """Empty strings must reach the caller so it can choose 'don't overwrite'."""
    raw = {
        "url": "https://vstrike.net",
        "username": "",
        "password": "",
    }
    secrets, _non_secrets = split_secrets("vstrike", raw)
    assert secrets["VSTRIKE_USERNAME"] == ""
    assert secrets["VSTRIKE_PASSWORD"] == ""


def test_split_secrets_unregistered_integration_passthrough():
    """Unregistered integrations get an empty secrets dict + a copy of input."""
    raw = {"foo": "bar", "baz": 42}
    secrets, non_secrets = split_secrets("brand-new", raw)
    assert secrets == {}
    assert non_secrets == raw
    assert non_secrets is not raw  # copy, not alias


def test_redact_secrets_removes_registered_fields():
    raw = {
        "url": "https://vstrike.net",
        "username": "alice",
        "password": "wonderland",
        "verify_ssl": True,
    }
    redacted = redact_secrets("vstrike", raw)
    assert "username" not in redacted
    assert "password" not in redacted
    assert redacted["url"] == "https://vstrike.net"
    assert redacted["verify_ssl"] is True


def test_redact_secrets_unregistered_integration_passthrough():
    raw = {"foo": "bar"}
    redacted = redact_secrets("brand-new", raw)
    assert redacted == raw


def test_secret_field_names_returns_form_field_names():
    names = list(secret_field_names("vstrike"))
    assert set(names) == {"username", "password"}


def test_registry_is_a_mapping_not_a_dict_alias():
    """Sanity: callers shouldn't be able to mutate the registry by accident."""
    # `secret_fields_for` returns the registry's inner mapping by reference.
    # We don't enforce immutability here, but flag it if a caller mutates.
    fields = secret_fields_for("vstrike")
    original_size = len(fields)
    # Constructing a new dict from it is fine; the registry stays intact.
    {**fields, "extra": "ENV"}
    assert len(secret_fields_for("vstrike")) == original_size
    # And the registry export is keyed by integration_id
    assert "vstrike" in INTEGRATION_SECRET_FIELDS


# ---------------------------------------------------------------------------
# Coverage of every password-typed integration in integrations.ts
# ---------------------------------------------------------------------------


def test_every_audited_integration_is_registered():
    """Sweep test: every integration with password-typed fields in the
    frontend metadata must have a corresponding registry entry, otherwise
    its credentials would still flow through the plaintext path on save.

    The list below was derived by parsing
    ``frontend/src/config/integrations.ts`` for ``type: 'password'`` fields
    grouped by their parent integration ``id``. If a new integration with
    password-typed fields is added to the frontend, add it here AND to
    ``_SECRET_FIELDS`` in services.integration_secrets.
    """
    expected = {
        "github",
        "virustotal",
        "alienvault-otx",
        "shodan",
        "misp",
        "gcp-threat-intel",
        "url-analysis",
        "ip-geolocation",
        "crowdstrike",
        "sentinelone",
        "carbon-black",
        "microsoft-defender",
        "cortex-xdr",
        "trend-micro-vision-one",
        "sophos-intercept-x",
        "cybereason",
        "trellix",
        "tanium",
        "cynet",
        "eset",
        "bitdefender-gravityzone",
        "fortinet-fortiedr",
        "kaspersky",
        "cisco-secure-endpoint",
        "symantec-edr",
        "splunk",
        "cribl-stream",
        "elastic-siem",
        "azure-sentinel",
        "qradar",
        "arcsight",
        "logrhythm",
        "exabeam",
        "securonix",
        "sumo-logic",
        "graylog",
        "aws-security-hub",
        "aws-guardduty",
        "gcp-security",
        "azure-defender",
        "prisma-cloud",
        "orca-security",
        "wiz",
        "lacework",
        "aqua-security",
        "snyk",
        "okta",
        "azure-ad",
        "ping-identity",
        "auth0",
        "onelogin",
        "duo-security",
        "jumpcloud",
        "sailpoint",
        "cyberark",
        "beyond-trust",
        "palo-alto",
        "cisco-firepower",
        "fortinet",
        "checkpoint",
        "zscaler",
        "sophos",
        "cloudflare",
        "cloudforce_one",
        "juniper-srx",
        "jira",
        "servicenow",
        "thehive",
        "cortex-xsoar",
        "swimlane",
        "ibm-resilient",
        "opsgenie",
        "slack",
        "pagerduty",
        "microsoft-teams",
        "email",
        "webhook",
        "discord",
        "mattermost",
        "hybrid-analysis",
        "joe-sandbox",
        "anyrun",
        "timesketch",
        "velociraptor",
        "grr",
        "autopsy",
        "osquery",
        "cuckoo",
        "vstrike",
    }
    missing = expected - set(INTEGRATION_SECRET_FIELDS.keys())
    assert not missing, f"Integrations missing from registry: {sorted(missing)}"


def test_default_naming_convention_for_well_known_integrations():
    """Sample of integrations that should follow ``<ID>_<FIELD>`` exactly."""
    cases = [
        ("virustotal", "api_key", "VIRUSTOTAL_API_KEY"),
        ("shodan", "api_key", "SHODAN_API_KEY"),
        ("github", "token", "GITHUB_TOKEN"),
        ("splunk", "password", "SPLUNK_PASSWORD"),
        ("sentinelone", "api_token", "SENTINELONE_API_TOKEN"),
        ("aws-security-hub", "secret_access_key", "AWS_SECURITY_HUB_SECRET_ACCESS_KEY"),
        ("microsoft-defender", "client_secret", "MICROSOFT_DEFENDER_CLIENT_SECRET"),
        ("cribl-stream", "password", "CRIBL_STREAM_PASSWORD"),
        ("cloudforce_one", "api_token", "CLOUDFORCE_ONE_API_TOKEN"),
    ]
    for integration_id, field, expected_env in cases:
        actual = INTEGRATION_SECRET_FIELDS[integration_id][field]
        assert (
            actual == expected_env
        ), f"{integration_id}.{field} expected {expected_env}, got {actual}"


def test_overrides_take_precedence_over_default_naming():
    """CrowdStrike's MCP server reads FALCON_*, not CROWDSTRIKE_*."""
    assert (
        INTEGRATION_SECRET_FIELDS["crowdstrike"]["client_secret"]
        == "FALCON_CLIENT_SECRET"
    )
    # PagerDuty mcp-config.json source placeholder is PAGERDUTY_API_KEY.
    assert INTEGRATION_SECRET_FIELDS["pagerduty"]["api_token"] == "PAGERDUTY_API_KEY"
    # The other PagerDuty secret falls back to the default convention.
    assert (
        INTEGRATION_SECRET_FIELDS["pagerduty"]["integration_key"]
        == "PAGERDUTY_INTEGRATION_KEY"
    )


def test_multi_secret_integrations_register_each_field():
    """Integrations with multiple password fields must register every one."""
    assert set(INTEGRATION_SECRET_FIELDS["trellix"].keys()) == {
        "client_secret",
        "api_key",
    }
    assert set(INTEGRATION_SECRET_FIELDS["zscaler"].keys()) == {
        "api_key",
        "password",
    }
    assert set(INTEGRATION_SECRET_FIELDS["timesketch"].keys()) == {
        "password",
        "api_token",
    }
    assert set(INTEGRATION_SECRET_FIELDS["pagerduty"].keys()) == {
        "api_token",
        "integration_key",
    }
    assert set(INTEGRATION_SECRET_FIELDS["vstrike"].keys()) == {
        "username",
        "password",
    }


def test_no_env_var_collisions_across_integrations():
    """No two registered (integration, field) pairs may share an env-var
    name unless that's an explicit override (e.g. shared keys across
    integrations would collide silently in the secrets store)."""
    seen: dict[str, tuple[str, str]] = {}
    collisions: list[str] = []
    for integration_id, fields in INTEGRATION_SECRET_FIELDS.items():
        for field, env_var in fields.items():
            if env_var in seen:
                prior = seen[env_var]
                collisions.append(
                    f"{env_var}: {prior[0]}.{prior[1]} vs {integration_id}.{field}"
                )
            else:
                seen[env_var] = (integration_id, field)
    assert not collisions, "Env-var name collisions: " + ", ".join(collisions)
