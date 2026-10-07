"""Every environment name the Helm chart renders must be one something reads.

Settings uses extra="ignore", so a chart key nothing reads lands in the pod
environment and is dropped without an error. The chart shipped
ORCHESTRATOR_MAX_CONCURRENT_AGENTS, VIGIL_OTEL_LOG_LLM_CONTENT and
ELASTIC_{HOST,API_KEY,...} for releases while the code read
ORCHESTRATOR_MAX_AGENTS and ELASTIC_SIEM_*, and operators who set them got no
limit and no Elastic connection.

Static parse of values.yaml, values-dev.yaml and the templates; no helm binary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.config import Settings
from core.integrations._base.descriptor import iter_descriptors
from core.integrations.integration_secrets import (
    INTEGRATION_SECRET_FIELDS,
    env_var_for,
)

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil"

# Names Settings and the integration resolver do not cover, each with its reader.
OTHER_CONSUMERS = {
    # Credentials read through get_secret, which falls back to the environment.
    "AGENT_INTERNAL_TOKEN": "api claude router, agent harness",
    "ANTHROPIC_API_KEY": "core.llm",
    "AWS_ACCESS_KEY_ID": "ingestion and config routers",
    "AWS_SECRET_ACCESS_KEY": "ingestion and config routers",
    "DARKTRACE_WEBHOOK_SECRET": "darktrace webhook router",
    "JWT_SECRET_KEY": "auth",
    "KAFKA_SASL_PASSWORD": "services/daemon/config.py",
    "KAFKA_SASL_USERNAME": "services/daemon/config.py",
    "OPENAI_API_KEY": "core.llm.bifrost.admin",
    "POSTGRES_PASSWORD": "core.storage.connection, agent db.ts",
    "SMTP_PASSWORD": "core.platform.email_service",
    "VSTRIKE_API_KEY": "vstrike client and router",
    "VSTRIKE_INBOUND_API_KEY": "vstrike router",
    # Bootstrap for the secrets manager, read before Settings exists.
    "ENABLE_KEYRING": "core.secrets_manager",
    "SECRETS_BACKEND": "core.secrets_manager",
    # Read by third-party SDKs.
    "AWS_REGION": "AWS SDKs",
    "OTEL_TRACES_SAMPLER": "OpenTelemetry SDK",
    "PYTHONUNBUFFERED": "CPython",
    # Interpolated into mcp-config.json for the VStrike MCP server.
    "VSTRIKE_BASE_URL": "vstrike MCP server",
    # TypeScript agent processes (services/agent).
    "VIGIL_PLAYBOOKS_URL": "agent layer",
    "VIGIL_PRICING_URL": "agent layer",
    "VIGIL_RUNS_URL": "agent layer",
    "VIGIL_TOOLS_URL": "agent layer",
}

# Old chart names, and what the code reads instead (None: nothing, the setting
# is gone). Kept in step with the retired-key warning in templates/NOTES.txt.
RETIRED = {
    "VIGIL_OTEL_LOG_LLM_CONTENT": None,
    "VIGIL_OTEL_RECORD_LLM_CONTENT": None,
    "ORCHESTRATOR_MAX_CONCURRENT_AGENTS": "ORCHESTRATOR_MAX_AGENTS",
    "ELASTIC_HOST": "ELASTIC_SIEM_ELASTICSEARCH_URL",
    "ELASTIC_KIBANA_URL": "ELASTIC_SIEM_KIBANA_URL",
    "ELASTIC_USERNAME": "ELASTIC_SIEM_USERNAME",
    "ELASTIC_VERIFY_SSL": "ELASTIC_SIEM_VERIFY_SSL",
    "ELASTIC_INDEX_PATTERN": "ELASTIC_SIEM_INDEX_PATTERN",
    "ELASTIC_API_KEY": "ELASTIC_SIEM_API_KEY",
    "ELASTIC_PASSWORD": "ELASTIC_SIEM_PASSWORD",
    "CROWDSTRIKE_CLIENT_SECRET": "FALCON_CLIENT_SECRET",
    "CRIBL_PASSWORD": "CRIBL_STREAM_PASSWORD",
    "PAGERDUTY_ROUTING_KEY": "PAGERDUTY_INTEGRATION_KEY",
    "TEAMS_WEBHOOK_URL": "MICROSOFT_TEAMS_WEBHOOK_URL",
}


def _derived_names() -> set[str]:
    names = {n.upper() for n in Settings.model_fields}
    for descriptor in iter_descriptors():
        names.update(env_var_for(descriptor.id, f) for f in descriptor.field_names)
    for fields in INTEGRATION_SECRET_FIELDS.values():
        names.update(fields.values())
    return names


def _read_names() -> set[str]:
    return _derived_names() | OTHER_CONSUMERS.keys()


def _config_keys(values_file: str) -> set[str]:
    values = yaml.safe_load((CHART / values_file).read_text(encoding="utf-8"))
    return set(values.get("config", {})) | set(values.get("extraConfig", {}))


def _template_keys() -> set[str]:
    # Keys of the Secret's stringData, and the ConfigMap's computed overrides.
    secret = (CHART / "templates" / "secret.yaml").read_text(encoding="utf-8")
    configmap = (CHART / "templates" / "configmap.yaml").read_text(encoding="utf-8")
    return set(re.findall(r"^  ([A-Z][A-Z0-9_]*):", secret, re.M)) | set(
        re.findall(r'set \$computed "([A-Z][A-Z0-9_]*)"', configmap)
    )


def _unread(keys: set[str]) -> list[str]:
    return sorted(keys - _read_names())


@pytest.mark.parametrize("values_file", ["values.yaml", "values-dev.yaml"])
def test_config_keys_are_read(values_file: str) -> None:
    unread = _unread(_config_keys(values_file))
    assert not unread, (
        f"{values_file} config keys nothing reads (renamed? see RETIRED): {unread}. "
        "Use the name the code reads, or add the key to OTHER_CONSUMERS with its reader."
    )


def test_rendered_template_keys_are_read() -> None:
    unread = _unread(_template_keys())
    assert not unread, f"Secret/ConfigMap template keys nothing reads: {unread}"


def test_secret_template_renders_elastic_under_the_names_the_code_reads() -> None:
    assert {"ELASTIC_SIEM_API_KEY", "ELASTIC_SIEM_PASSWORD"} <= _template_keys()


def test_reintroducing_a_retired_name_fails_the_check() -> None:
    assert _unread({"VIGIL_OTEL_LOG_LLM_CONTENT"}) == ["VIGIL_OTEL_LOG_LLM_CONTENT"]


def test_retired_names_map_to_names_the_code_reads() -> None:
    read = _read_names()
    assert not set(RETIRED) & read, "a RETIRED name is read again; drop it"
    assert {v for v in RETIRED.values() if v} <= read


def test_notes_warn_about_every_retired_name() -> None:
    notes = (CHART / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    missing = [k for k in RETIRED if k not in notes]
    assert not missing, f"NOTES.txt does not warn about retired keys: {missing}"


def test_other_consumers_has_no_redundant_entries() -> None:
    redundant = sorted(OTHER_CONSUMERS.keys() & _derived_names())
    assert not redundant, f"already covered by Settings or a descriptor: {redundant}"
