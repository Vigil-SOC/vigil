import re
from pathlib import Path

import pytest

from core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / "env.example"

# Keys env.example documents that are deliberately NOT Settings fields, grouped
# by the channel that owns them. Anything not listed here must become a field.
NOT_SETTINGS = {
    # Credentials — the encrypted store owns these, read via get_secret so a
    # value saved in the UI wins over the environment.
    "AGENT_INTERNAL_TOKEN",
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
    "ELASTIC_API_KEY",
    "ELASTIC_PASSWORD",
    "ELASTIC_USERNAME",
    "GITHUB_TOKEN",
    "JOE_SANDBOX_API_KEY",
    "JWT_SECRET_KEY",
    "KAFKA_SASL_PASSWORD",
    "KAFKA_SASL_USERNAME",
    "OPENAI_API_KEY",
    "PAGERDUTY_ROUTING_KEY",
    "POSTGRES_PASSWORD",
    "SHODAN_API_KEY",
    "SLACK_BOT_TOKEN",
    "SMTP_PASSWORD",
    "SPLUNK_PASSWORD",
    "SPLUNK_USERNAME",
    "TEAMS_WEBHOOK_URL",
    "TIMESKETCH_PASSWORD",
    "TIMESKETCH_USERNAME",
    "VIRUSTOTAL_API_KEY",
    "VSTRIKE_API_KEY",
    "VSTRIKE_INBOUND_API_KEY",
    "VSTRIKE_PASSWORD",
    "VSTRIKE_USERNAME",
    # Integration endpoints and options consumed inside MCP server child
    # processes, whose config protocol is the environment they are spawned with.
    "CLOUDFORCE_ONE_COLLECTION_IDS",
    "CLOUDFORCE_ONE_TAXII_SERVER_URL",
    "CRIBL_URL",
    "CRIBL_WORKER_GROUP",
    "CROWDSTRIKE_BASE_URL",
    "ELASTIC_HOST",
    "ELASTIC_INDEX_PATTERN",
    "ELASTIC_KIBANA_URL",
    "ELASTIC_PATHS",
    "ELASTIC_VERIFY_SSL",
    "KQL_PATHS",
    "SIGMA_PATHS",
    "SLACK_DEFAULT_CHANNEL",
    "SPLUNK_MCP_URL",
    "SPLUNK_PATHS",
    "SPLUNK_URL",
    "STORY_PATHS",
    "TIMESKETCH_URL",
    "VSTRIKE_BASE_URL",
    "VSTRIKE_VERIFY_SSL",
    # core.platform.runtime_config ENV_FALLBACKS: DB-first settings whose env var is
    # only the fallback when the system_config row is absent.
    "ANTHROPIC_PROMPT_CACHE_ENABLED",
    "CLAUDE_HISTORY_WINDOW",
    "CLAUDE_THINKING_BUDGET",
    "LOCAL_OLLAMA_RECOVERY_ENABLED",
    "LOCAL_OLLAMA_RECOVERY_RESTART_GATEWAY",
    "LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT",
    "TOOL_RESPONSE_BUDGET_DEFAULT",
    # Per-provider names built at runtime, so they cannot be static fields.
    "ANTHROPIC_EXTRA_MODELS",
    "OPENAI_EXTRA_MODELS",
    # Read by third-party SDKs and tooling, not by Vigil code.
    "AWS_REGION",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    # Consumed outside the Python backend (shell scripts, compose, Vite).
    "BIND_HOST",
    "GRAFANA_PASSWORD",
    "VITE_EXTENSION_ORIGIN_ALLOWLIST",
    # Read by the TypeScript agent processes themselves, not by Settings.
    "AGENT_HEALTH_PORT",
    "AGENT_HTTP_PORT",
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
    # Provider selection still handled by the DB provider registry.
    "DEFAULT_LLM_PROVIDER",
    "OPENAI_BASE_URL",
    "OPENAI_ENABLED",
    "OPENAI_ORGANIZATION",
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
