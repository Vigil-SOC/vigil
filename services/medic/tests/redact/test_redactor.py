"""K2 (minimal): the pattern-only redactor against the contract fixtures."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from services.medic.redact import REDACTION_VERSION, Redactor
from services.medic.redact.secret_names import NAMES

MEDIC = Path(__file__).resolve().parents[2]
CASES = json.loads((MEDIC / "contracts/fixtures/redaction/cases.json").read_text())
REPO = MEDIC.parents[1]

REDACTOR = Redactor()


def test_every_redact_awk_family_has_a_case() -> None:
    families = {c["family"] for c in CASES["must_redact"]}
    assert families == {
        "key_name",
        "url_credential",
        "pem",
        "token_shape",
        "flag",
        "contextual_token",
        "cookie",
        "redis_auth",
        "variable_reference",
    }


@pytest.mark.parametrize("case", CASES["must_redact"], ids=lambda c: c["id"])
def test_must_redact(case: dict) -> None:
    out, hits = REDACTOR.redact(case["input"])
    assert case["secret"] not in out
    assert out == case["expected"]
    assert hits >= 1


@pytest.mark.parametrize("line", CASES["must_keep"])
def test_must_keep(line: str) -> None:
    assert REDACTOR.redact(line) == (line, 0)


def test_replacement_is_fixed_text_with_no_length_or_fragment() -> None:
    short, _ = REDACTOR.redact("password=abcdefgh")
    long, _ = REDACTOR.redact("password=" + "x" * 300)
    assert short == long == "password=[REDACTED]"


def test_already_redacted_text_is_stable() -> None:
    once, _ = REDACTOR.redact("GET /reset?token=abc123def456 token zzz999yyy888")
    assert REDACTOR.redact(once) == (once, 0)


def test_version_is_schema_safe() -> None:
    import re

    assert re.fullmatch(r"[a-z0-9.-]{1,32}", REDACTION_VERSION)


def test_secret_names_copy_matches_the_support_bundle_list() -> None:
    # Medic can't read scripts/ at runtime (its image ships services/medic/ only),
    # so it keeps a copy; this pins the copy to the original while both live here.
    theirs = REPO / "scripts/vigil-support/secret-names.txt"
    if not theirs.exists():
        pytest.skip("not in the Vigil repo")
    assert list(NAMES) == [
        n.strip() for n in theirs.read_text().splitlines() if n.strip()
    ]


def test_redact_ships_only_python() -> None:
    # The image build drops *.txt (.dockerignore); a data file here would be
    # missing at runtime and Medic would die at import.
    assert {p.suffix for p in (MEDIC / "redact").iterdir() if p.is_file()} == {".py"}


# Sentinel (integration, field, env) triples from tests/unit/test_support_redact_awk.py.
# Copied, not imported: that module imports core, and Medic's fence forbids it.
_SENTINELS = (
    ("alienvault-otx", "api_key", "ALIENVAULT_OTX_API_KEY"),
    ("anyrun", "api_key", "ANYRUN_API_KEY"),
    ("aws-security-hub", "secret_access_key", "AWS_SECURITY_HUB_SECRET_ACCESS_KEY"),
    ("azure-ad", "client_secret", "AZURE_AD_CLIENT_SECRET"),
    ("azure-sentinel", "client_secret", "AZURE_SENTINEL_CLIENT_SECRET"),
    ("cape-sandbox", "api_key", "CAPE_SANDBOX_API_KEY"),
    ("carbon-black", "api_key", "CARBON_BLACK_API_KEY"),
    ("cloudflare", "api_token", "CLOUDFLARE_API_TOKEN"),
    ("cloudforce_one", "api_token", "CLOUDFORCE_ONE_API_TOKEN"),
    ("cribl-stream", "password", "CRIBL_STREAM_PASSWORD"),
    ("crowdstrike", "client_secret", "FALCON_CLIENT_SECRET"),
    ("elastic-siem", "api_key", "ELASTIC_SIEM_API_KEY"),
    ("elastic-siem", "password", "ELASTIC_SIEM_PASSWORD"),
    ("elastic-siem", "proxy_password", "ELASTIC_SIEM_PROXY_PASSWORD"),
    ("firecrawl", "api_key", "FIRECRAWL_API_KEY"),
    ("gcp-threat-intel", "api_key", "GCP_THREAT_INTEL_API_KEY"),
    ("github", "token", "GITHUB_TOKEN"),
    ("hybrid-analysis", "api_key", "HYBRID_ANALYSIS_API_KEY"),
    ("ip-geolocation", "api_key", "IP_GEOLOCATION_API_KEY"),
    ("jira", "api_token", "JIRA_API_TOKEN"),
    ("joe-sandbox", "api_key", "JOE_SANDBOX_API_KEY"),
    ("loglm", "mcp_token", "LOGLM_MCP_TOKEN"),
    ("loglm", "mint_secret", "LOGLM_MINT_SECRET"),
    ("microsoft-defender", "client_secret", "MICROSOFT_DEFENDER_CLIENT_SECRET"),
    ("microsoft-teams", "webhook_url", "MICROSOFT_TEAMS_WEBHOOK_URL"),
    ("misp", "api_key", "MISP_API_KEY"),
    ("misp", "proxy_password", "MISP_PROXY_PASSWORD"),
    ("okta", "api_token", "OKTA_API_TOKEN"),
    ("opensearch", "password", "OPENSEARCH_PASSWORD"),
    ("opensearch", "proxy_password", "OPENSEARCH_PROXY_PASSWORD"),
    ("pagerduty", "api_token", "PAGERDUTY_API_KEY"),
    ("pagerduty", "integration_key", "PAGERDUTY_INTEGRATION_KEY"),
    ("palo-alto", "api_key", "PALO_ALTO_API_KEY"),
    ("sentinelone", "api_token", "SENTINELONE_API_TOKEN"),
    ("shodan", "api_key", "SHODAN_API_KEY"),
    ("slack", "bot_token", "SLACK_BOT_TOKEN"),
    ("splunk", "password", "SPLUNK_PASSWORD"),
    ("splunk", "proxy_password", "SPLUNK_PROXY_PASSWORD"),
    ("virustotal", "api_key", "VIRUSTOTAL_API_KEY"),
    ("vstrike", "password", "VSTRIKE_PASSWORD"),
    ("vstrike", "username", "VSTRIKE_USERNAME"),
)


def _const_str(node: ast.AST) -> str:
    """String constants plus the `+` and `*` the CONTROLS list uses."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _const_str(node.left) + _const_str(node.right)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, int)
    ):
        return _const_str(node.left) * node.right.value
    raise AssertionError(f"CONTROLS entry is not a string constant: {ast.dump(node)}")


def _awk_controls() -> list[str]:
    """CONTROLS from the support-bundle test, loaded without importing core."""
    path = REPO / "tests/unit/test_support_redact_awk.py"
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "CONTROLS":
                if not isinstance(node.value, ast.List):
                    break
                return [_const_str(elt) for elt in node.value.elts]
    raise AssertionError(f"CONTROLS missing from {path}")


def _camel(env: str) -> str:
    head, *rest = env.lower().split("_")
    return head + "".join(word.capitalize() for word in rest)


def _surfaces(env: str, secret: str) -> list[str]:
    # Same lines as tests/unit/test_support_redact_awk.py surfaces().
    return [
        f"{env}={secret}",
        f'export {env}="{secret}"',
        f"services:\n  api:\n    environment:\n      {env}: {secret}\n      - {env}={secret}",
        f'  {_camel(env)}: "{secret}"',
        f'{{"{env.lower()}": "{secret}", "debug": false}}',
        f"postgresql://vigil:{secret}@db.internal:5432/vigil",
        f'INFO:     10.0.0.5:4242 - "GET /api/config?{env}={secret} HTTP/1.1" 200 OK',
        f"2026-10-05 12:00:00,123 - vigil.daemon - ERROR - connector failed {env}={secret}",
        f'[agent] worker 3 call failed: {{"{env}":"{secret}"}} retrying',
        f"- name: {env}\n  value: {secret}",
    ]


def test_parity_with_redact_awk_controls_and_sentinels() -> None:
    """0 sentinel leaks, and no control or sentinel line awk keeps is rewritten."""
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("awk is not installed")
    controls = _awk_controls()
    parts: list[str] = []
    secrets: list[str] = []
    for integration, field, env in _SENTINELS:
        secret = f"s3ntinel-{integration}-{field}-Qx7"
        secrets.append(secret)
        parts.extend(_surfaces(env, secret))
    parts.extend(controls)
    text = "\n".join(parts) + "\n"
    script = REPO / "scripts/vigil-support/redact.awk"
    names = REPO / "scripts/vigil-support/secret-names.txt"
    out = subprocess.run(
        [awk, "-f", str(script), "-v", f"names={names}"],
        input=text.encode(),
        capture_output=True,
        check=True,
    ).stdout.decode()
    awk_lines = out.splitlines()
    med_lines = REDACTOR.redact(text)[0].splitlines()
    orig_lines = text.splitlines()
    assert len(awk_lines) == len(orig_lines) == len(med_lines)
    leaks = [
        (i, line)
        for i, line in enumerate(med_lines)
        if any(secret in line for secret in secrets)
    ]
    over = [
        (orig, med)
        for orig, awk_line, med in zip(orig_lines, awk_lines, med_lines, strict=True)
        if awk_line == orig and med != orig
    ]
    assert leaks == []
    assert over == []
    assert len(secrets) == 41


def test_huge_input_is_capped_before_redaction() -> None:
    from services.medic.redact.choke import redact_observation

    obs = {"values": [{"key": "d", "type": "text", "value": "x " * 200_000}]}
    out = redact_observation(obs, REDACTOR)
    assert len(out["values"][0]["value"]) == 500
