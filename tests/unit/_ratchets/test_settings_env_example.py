import re
from pathlib import Path

import pytest

from core.config import Settings
from core.integrations.integration_secrets import ENV_CREDENTIAL_NAMES

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / "env.example"

# Keys env.example documents that are deliberately NOT Settings fields, grouped
# by the channel that owns them. Anything not listed here must become a field.
NOT_SETTINGS = ENV_CREDENTIAL_NAMES | {
    # Integration endpoints and options consumed inside MCP server child
    # processes, whose config protocol is the environment they are spawned with.
    "ATOMIC_RED_TEAM_ATOMICS_PATH",
    "ATOMIC_RED_TEAM_RUNNER_PATH",
    "CLOUDFORCE_ONE_COLLECTION_IDS",
    "CLOUDFORCE_ONE_TAXII_SERVER_URL",
    "CRIBL_URL",
    "CRIBL_WORKER_GROUP",
    "CROWDSTRIKE_BASE_URL",
    "ELASTIC_PATHS",
    "ELASTIC_SIEM_ELASTICSEARCH_URL",
    "ELASTIC_SIEM_INDEX_PATTERN",
    "ELASTIC_SIEM_KIBANA_URL",
    "ELASTIC_SIEM_MIN_RULE_LEVEL",
    "ELASTIC_SIEM_VERIFY_SSL",
    "OPENSEARCH_DASHBOARDS_URL",
    "OPENSEARCH_INDEX_PATTERN",
    "OPENSEARCH_OPENSEARCH_URL",
    "OPENSEARCH_VERIFY_SSL",
    "KQL_PATHS",
    "SIGMA_PATHS",
    "SLACK_DEFAULT_CHANNEL",
    "SPLUNK_MCP_URL",
    "SPLUNK_PATHS",
    "SPLUNK_URL",
    "SPLUNK_VERIFY_SSL",
    "STORY_PATHS",
    "VSTRIKE_BASE_URL",
    "VSTRIKE_VERIFY_SSL",
    # Per-integration CA paths, resolved like the fields above.
    "ELASTIC_SIEM_CA_CERT_PATH",
    "OPENSEARCH_CA_CERT_PATH",
    "MISP_CA_CERT_PATH",
    "PALO_ALTO_CA_CERT_PATH",
    "SPLUNK_CA_CERT_PATH",
    "VSTRIKE_CA_CERT_PATH",
    # core.platform.runtime_config ENV_FALLBACKS: DB-first settings whose env var is
    # only the fallback when the system_config row is absent.
    "LOCAL_OLLAMA_RECOVERY_ENABLED",
    "LOCAL_OLLAMA_RECOVERY_RESTART_GATEWAY",
    "LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT",
    # Per-provider names built at runtime, so they cannot be static fields.
    "ANTHROPIC_EXTRA_MODELS",
    "OPENAI_EXTRA_MODELS",
    # Read by third-party SDKs and tooling, not by Vigil code.
    "AWS_REGION",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    # TLS trust for inspecting proxies. Python and Node read these directly;
    # ca_bundle_env() forwards them to MCP children. Not Settings fields.
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    # Consumed outside the Python backend (shell scripts, compose, Vite).
    "BIFROST_IMAGE_TAG",
    "BIND_HOST",
    "GRAFANA_PASSWORD",
    "VITE_EXTENSION_ORIGIN_ALLOWLIST",
    # Read by the TypeScript agent processes themselves, not by Settings.
    "AGENT_HEALTH_PORT",
    "AGENT_HTTP_PORT",
    # Agent (services/agent/core/db.ts) and scripts/migrate_schema.py.
    # Python DatabaseConfig reads the encrypted DSN / POSTGRES_* instead (#752).
    "DATABASE_URL",
    # The agent worker's Redis parts. Python has no equivalent -- it reads
    # REDIS_URL, which is a Setting.
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "VIGIL_PLAYBOOKS_URL",
    "VIGIL_PRICING_URL",
    "VIGIL_RUNS_URL",
    "VIGIL_TOOLS_URL",
    # Bootstrap for the secrets manager itself, which cannot depend on Settings.
    "ENABLE_KEYRING",
    "SECRETS_BACKEND",
    # Locates the State Directory. vigil_path() resolves it at import time, before
    # Settings can be built, so it is read from the environment and must be
    # exported rather than set in .env.
    "VIGIL_DIR",
}


def _documented_keys() -> set:
    # A commented-out "# KEY=default" line is documentation too, and is how
    # env.example records optional knobs.
    keys = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z_0-9]*)=", line.strip())
        if match:
            keys.add(match.group(1))
    return keys


@pytest.mark.unit
def test_every_setting_is_documented():
    undocumented = sorted(
        {n.upper() for n in Settings.model_fields} - _documented_keys()
    )
    assert not undocumented, (
        "Settings fields missing from env.example. Every knob must be "
        "discoverable there:\n  " + "\n  ".join(undocumented)
    )


@pytest.mark.unit
def test_every_documented_key_is_a_setting_or_declared_otherwise():
    fields = {n.upper() for n in Settings.model_fields}
    orphans = sorted(_documented_keys() - fields - NOT_SETTINGS)
    assert not orphans, (
        "env.example documents keys that are neither Settings fields nor listed "
        "in NOT_SETTINGS. Add the field, or record which channel owns it:\n  "
        + "\n  ".join(orphans)
    )


@pytest.mark.unit
def test_no_stale_entries_in_the_exclusion_list():
    # Keeps NOT_SETTINGS honest: an entry that leaves env.example, or becomes a
    # real Settings field, must be removed from the list.
    fields = {n.upper() for n in Settings.model_fields}
    documented = _documented_keys()
    stale = sorted(k for k in NOT_SETTINGS if k not in documented or k in fields)
    assert not stale, "NOT_SETTINGS entries no longer apply:\n  " + "\n  ".join(stale)
