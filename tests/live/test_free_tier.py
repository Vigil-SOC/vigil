"""Live checks for the connectors that run on a free vendor tier.

Every probe is one read-only call against a stable public object (or, for
account-bound vendors, the account's own identity) and asserts on a field that
only a working connector can fill. An auth error, an error payload, or an empty
answer fails. ``ACCOUNTS.md`` says which free plan, token, and secret each probe
needs; a probe whose credentials are absent is skipped as not configured.

Tool names and argument shapes come from each server's real ``tools/list`` at
the version ``mcp-config.json`` pins.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from tests.live.harness import Probe, check


def _text(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload)


def _find(payload: Any, key: str) -> Any:
    """First value stored under ``key`` anywhere in a decoded payload."""
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        payload = list(payload.values())
    if isinstance(payload, list):
        for item in payload:
            found = _find(item, key)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# VirusTotal. The public API allows 4 requests/min and 500/day.
#
# get_domain_report with ``relationships=[]`` is one request to GET /domains/{d}.
# Its default (no argument) and get_file_report/get_ip_report/get_url_report all
# fan out over relationships such as contacted_domains and embedded_ips, which
# VirusTotal documents as premium-only, so those tools are not checked here.
# ---------------------------------------------------------------------------


def _virustotal_domain(payload: Any) -> str:
    text = _text(payload)
    assert "Domain: google.com" in text, f"not a google.com report: {text[:300]}"
    assert "Detection Results" in text, f"no analysis stats in the report: {text[:300]}"
    return "google.com report with analysis stats"


# ---------------------------------------------------------------------------
# Shodan. Any free account has an API key. dns_lookup is GET /dns/resolve, which
# 401s without a valid key (checked) and is not the credit-metered part of the
# API (search filters are). ip_lookup (/shodan/host/{ip}) and cve_lookup
# (cvedb.shodan.io) are not probed: both answer with no key or a bad one, so a
# pass would say nothing about the credential.
# ---------------------------------------------------------------------------


def _shodan_dns(payload: Any) -> str:
    assert isinstance(payload, dict), f"unexpected answer: {_text(payload)[:300]}"
    rows = {r["Hostname"]: r["IP Address"] for r in payload.get("DNS Resolutions") or []}
    assert rows.get("dns.google") in {"8.8.8.8", "8.8.4.4"}, f"dns.google resolved to {rows}"
    return f"dns.google -> {rows['dns.google']}"


# ---------------------------------------------------------------------------
# Firecrawl. The free plan is 1,000 credits a month; a scrape costs 1.
# ---------------------------------------------------------------------------


def _firecrawl_scrape(payload: Any) -> str:
    text = _text(payload)
    assert "Example Domain" in text, f"example.com was not scraped: {text[:300]}"
    return "scraped example.com"


# ---------------------------------------------------------------------------
# PagerDuty. get_user_data is GET /users/me with the user API token, so it
# answers on a brand-new account with the token owner's own record.
# ---------------------------------------------------------------------------


def _pagerduty_me(payload: Any) -> str:
    email, user_id = _find(payload, "email"), _find(payload, "id")
    assert email and "@" in str(email), f"no user email in the answer: {_text(payload)[:300]}"
    assert user_id, f"no user id in the answer: {_text(payload)[:300]}"
    return f"user {user_id}"


# ---------------------------------------------------------------------------
# Okta. Any org has at least the admin who minted the token, so a one-user
# listing is never empty. The server answers a failed call with an error string
# rather than {"users": [...]}, which the shape check turns into a failure.
# ---------------------------------------------------------------------------


def _okta_users(payload: Any) -> str:
    assert isinstance(payload, dict) and payload.get("users"), (
        f"no users in the answer: {_text(payload)[:300]}"
    )
    user = payload["users"][0]
    login = (user.get("profile") or {}).get("login")
    assert user.get("id") and login, f"user record has no id/login: {_text(user)[:300]}"
    return f"user {login}"


# ---------------------------------------------------------------------------
# GCP Security Command Center. The server uses Application Default Credentials;
# in CI ``google-github-actions/auth`` (Workload Identity Federation) exports
# GOOGLE_APPLICATION_CREDENTIALS. The server reports failures inside a dict, so
# the check is on shape: a permission or project error carries "error", and a
# real answer carries "top_findings" (an empty list is a valid answer: the
# probe proves auth and the SCC API, not that the project has findings).
# ---------------------------------------------------------------------------


def _scc_findings(payload: Any) -> str:
    assert isinstance(payload, dict), f"unexpected answer: {_text(payload)[:300]}"
    assert not payload.get("error"), f"{payload.get('error')}: {payload.get('details', '')[:300]}"
    assert isinstance(payload.get("top_findings"), list), f"no findings list: {_text(payload)[:300]}"
    return f"{payload.get('count')} findings"


# ---------------------------------------------------------------------------
# AWS Well-Architected security server. CheckSecurityServices with only
# accessanalyzer is one IAM Access Analyzer ListAnalyzers call. The server
# swallows AWS errors into the per-service dict ("error"), so the check is that
# the service answered with a verdict rather than an error.
# ---------------------------------------------------------------------------


def _aws_access_analyzer(payload: Any) -> str:
    assert isinstance(payload, dict), f"unexpected answer: {_text(payload)[:300]}"
    assert not payload.get("error"), f"{payload.get('error')}"
    status = (payload.get("service_statuses") or {}).get("accessanalyzer")
    assert isinstance(status, dict), f"no accessanalyzer status: {_text(payload)[:300]}"
    assert not status.get("error"), f"accessanalyzer call failed: {status.get('error')}"
    assert isinstance(status.get("enabled"), bool), f"no verdict: {_text(status)[:300]}"
    return f"accessanalyzer enabled={status['enabled']}"


PROBES = [
    Probe(
        "virustotal",
        "get_domain_report",
        {"domain": "google.com", "relationships": []},
        expect=_virustotal_domain,
    ),
    Probe("shodan", "dns_lookup", {"hostnames": ["dns.google"]}, expect=_shodan_dns),
    Probe(
        "firecrawl",
        "firecrawl_scrape",
        {"url": "https://example.com", "formats": ["markdown"]},
        expect=_firecrawl_scrape,
        timeout=90.0,
    ),
    Probe("pagerduty", "get_user_data", {}, expect=_pagerduty_me),
    # Needs OKTA_DOMAIN and OKTA_API_TOKEN; the first uvx fetch clones and installs.
    Probe("okta", "list_okta_users", {"max_results": 1}, expect=_okta_users, timeout=120.0),
]


@pytest.mark.parametrize("probe", PROBES, ids=repr)
async def test_connector(probe, mcp_client, request):
    await check(probe, mcp_client, request)


# ---------------------------------------------------------------------------
# File-based cloud credentials stay with stdio children. The ADC JSON that
# google-github-actions/auth writes, and the AWS shared-credentials profile
# below, are files on the job host that a runner child (its own uid and HOME)
# cannot read; #2303 leaves file-based credentials out of the runner epic, so
# these two run as stdio children and the matrix shows them as `stdio`.
# ---------------------------------------------------------------------------


@pytest.fixture
def stdio_only(monkeypatch):
    monkeypatch.delenv("MCP_RUNNER_URL", raising=False)


GCP_SCC = Probe(
    "gcp-scc",
    "top_vulnerability_findings",
    {"project_id": os.environ.get("LIVE_GCP_SCC_PROJECT_ID", ""), "max_findings": 1},
    expect=_scc_findings,
    requires=("GOOGLE_APPLICATION_CREDENTIALS", "LIVE_GCP_SCC_PROJECT_ID"),
    timeout=120.0,
)


@pytest.mark.parametrize("probe", [GCP_SCC], ids=repr)
async def test_gcp_scc(probe, stdio_only, mcp_client, request):
    await check(probe, mcp_client, request)


# ---------------------------------------------------------------------------
# Cloudflare, in-repo. The server resolves its token and account from the
# environment (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID). The probe still
# gives this process a throwaway VIGIL_DIR whose config enables it, as Settings
# would: resolve() runs here and pushes what it finds to the server.
#
# cf_lookup_ip_threat is GET /accounts/{id}/firewall/access_rules/rules. It is
# read-only and needs the token's "Account Firewall Access Rules: Read"
# permission; a new free account has no rules, so the answer is
# {"success": true, "rule_count": 0}. success=true is the evidence: the tool
# reports success=false (without an "error" key) on any API failure.
# ---------------------------------------------------------------------------


def _cloudflare_rules(payload: Any) -> str:
    assert isinstance(payload, dict), f"unexpected answer: {_text(payload)[:300]}"
    assert payload.get("success") is True, f"Cloudflare API call failed: {_text(payload)[:300]}"
    assert payload.get("ip") == "1.1.1.1", f"wrong lookup: {_text(payload)[:300]}"
    assert isinstance(payload.get("rule_count"), int), f"no rule_count: {_text(payload)[:300]}"
    return f"account rules readable ({payload['rule_count']} matching 1.1.1.1)"


CLOUDFLARE = Probe(
    "cloudflare",
    "cf_lookup_ip_threat",
    {"ip": "1.1.1.1"},
    expect=_cloudflare_rules,
    requires=("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
)


@pytest.fixture
def cloudflare_enabled(monkeypatch, tmp_path):
    (tmp_path / "integrations_config.json").write_text(
        json.dumps({"enabled_integrations": ["cloudflare"], "integrations": {"cloudflare": {}}})
    )
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))


@pytest.mark.parametrize("probe", [CLOUDFLARE], ids=repr)
async def test_cloudflare(probe, cloudflare_enabled, mcp_client, request):
    await check(probe, mcp_client, request)


# ---------------------------------------------------------------------------
# AWS. The server builds every session as boto3.Session(profile_name=...), which
# never reads plain AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (an explicit profile
# switches the env credential provider off, and a missing profile raises
# ProfileNotFound). mcp-config.json also gates the entry on AWS_PROFILE. So
# credentials from aws-actions/configure-aws-credentials are written into a
# throwaway shared-credentials profile here, which is what an operator's
# ~/.aws/credentials is. An AWS_PROFILE the runner already set wins.
# ---------------------------------------------------------------------------

AWS_SECURITY = Probe(
    "aws-security",
    "CheckSecurityServices",
    {"region": "us-east-1", "services": ["accessanalyzer"]},
    expect=_aws_access_analyzer,
    timeout=120.0,
)


@pytest.fixture
def aws_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_REGION", os.environ.get("AWS_REGION") or "us-east-1")
    if os.environ.get("AWS_PROFILE"):
        return
    key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (key and secret):
        pytest.skip("not configured: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
    lines = ["[vigil-live]", f"aws_access_key_id = {key}", f"aws_secret_access_key = {secret}"]
    if os.environ.get("AWS_SESSION_TOKEN"):
        lines.append(f"aws_session_token = {os.environ['AWS_SESSION_TOKEN']}")
    credentials = tmp_path / "credentials"
    credentials.write_text("\n".join(lines) + "\n")
    credentials.chmod(0o600)
    config = tmp_path / "config"
    config.write_text("")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_PROFILE", "vigil-live")


@pytest.mark.parametrize("probe", [AWS_SECURITY], ids=repr)
async def test_aws_security(probe, stdio_only, aws_profile, mcp_client, request):
    await check(probe, mcp_client, request)
