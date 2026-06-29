"""Tests for Cloudflare/Cloudforce One integration.

Covers:
- tools/cloudflare.py — the MCP server fails closed (no-op + clear error)
  when the integration is disabled, and the REST helpers shape arguments
  correctly when enabled (Cloudflare API itself is mocked).
- services/threat_feed_service.py — STIX 2.1 indicator parsing.
- backend/api/cloudflare_webhooks.py — the Cloudy receiver returns 503
  when CLOUDY_INGESTION_ENABLED is unset.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# tools/cloudflare.py — REST helpers + disabled-integration behavior
# ---------------------------------------------------------------------------


def _import_cloudflare_tool():
    """Load tools/cloudflare.py without importing tools/__init__ side effects."""
    spec = importlib.util.spec_from_file_location(
        "cloudflare_tool_under_test", _REPO_ROOT / "tools" / "cloudflare.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cloudflare_tool_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_config_returns_none_when_integration_disabled():
    cf = _import_cloudflare_tool()
    with patch.object(cf, "is_integration_enabled", return_value=False):
        assert cf._config() is None


def test_config_returns_none_when_token_missing():
    cf = _import_cloudflare_tool()
    with patch.object(cf, "is_integration_enabled", return_value=True), patch.object(
        cf, "get_integration_config", return_value={"account_id": "abc"}
    ):
        assert cf._config() is None


def test_waf_block_ip_requires_account_id():
    cf = _import_cloudflare_tool()
    out = cf._waf_block_ip(
        api_token="t", account_id=None, ip="1.2.3.4", reason="test"
    )
    assert out == {"error": "account_id required for WAF IP Access Rules"}


def test_waf_block_ip_posts_correct_payload():
    cf = _import_cloudflare_tool()
    fake = MagicMock()
    fake.status_code = 200
    fake.content = b"{}"
    fake.json.return_value = {"success": True, "result": {"id": "rule-1"}}

    with patch.object(cf.requests, "post", return_value=fake) as posted:
        out = cf._waf_block_ip(
            api_token="tok",
            account_id="acct-1",
            ip="9.9.9.9",
            reason="malicious",
        )
    assert out["success"] is True
    assert out["rule_id"] == "rule-1"
    args, kwargs = posted.call_args
    assert "/accounts/acct-1/firewall/access_rules/rules" in args[0]
    assert kwargs["json"]["configuration"] == {"target": "ip", "value": "9.9.9.9"}
    assert kwargs["json"]["mode"] == "block"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_gateway_block_domain_builds_traffic_filter():
    cf = _import_cloudflare_tool()
    fake = MagicMock()
    fake.status_code = 200
    fake.content = b"{}"
    fake.json.return_value = {"success": True, "result": {"id": "gw-1"}}
    with patch.object(cf.requests, "post", return_value=fake) as posted:
        out = cf._gateway_block_domain(
            api_token="tok",
            account_id="acct-1",
            domain="evil.example",
            reason="C2",
            rule_name=None,
        )
    assert out["success"] is True
    payload = posted.call_args.kwargs["json"]
    assert "evil.example" in payload["traffic"]
    assert payload["action"] == "block"
    assert "dns" in payload["filters"] and "http" in payload["filters"]


# ---------------------------------------------------------------------------
# services/threat_feed_service.py — STIX 2.1 parsing
# ---------------------------------------------------------------------------


def test_parse_stix_indicator_extracts_ipv4():
    from services.threat_feed_service import parse_stix_indicator

    obj = {
        "type": "indicator",
        "pattern": "[ipv4-addr:value = '203.0.113.5']",
        "confidence": 80,
        "labels": ["malicious-activity"],
        "valid_from": "2026-04-01T00:00:00Z",
    }
    out = parse_stix_indicator(obj, source="cloudforce_one", collection_id="c1")
    assert len(out) == 1
    ind = out[0]
    assert ind.indicator_type == "ip"
    assert ind.indicator_value == "203.0.113.5"
    assert ind.confidence == 80.0
    assert ind.threat_level == "high"
    assert ind.source == "cloudforce_one"
    assert ind.collection_id == "c1"


def test_parse_stix_indicator_handles_or_pattern():
    from services.threat_feed_service import parse_stix_indicator

    obj = {
        "type": "indicator",
        "pattern": "[domain-name:value = 'a.example' OR domain-name:value = 'b.example']",
    }
    out = parse_stix_indicator(obj, source="cloudforce_one", collection_id=None)
    assert sorted(i.indicator_value for i in out) == ["a.example", "b.example"]
    assert all(i.indicator_type == "domain" for i in out)


def test_parse_stix_indicator_skips_non_indicator():
    from services.threat_feed_service import parse_stix_indicator

    assert parse_stix_indicator({"type": "malware"}, source="x", collection_id=None) == []


# ---------------------------------------------------------------------------
# backend/api/cloudflare_webhooks.py — gating
# ---------------------------------------------------------------------------


def _load_webhook_module():
    spec = importlib.util.spec_from_file_location(
        "cloudflare_webhooks_under_test",
        _BACKEND_DIR / "api" / "cloudflare_webhooks.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cloudflare_webhooks_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gated_app(monkeypatch):
    monkeypatch.delenv("CLOUDY_INGESTION_ENABLED", raising=False)
    mod = _load_webhook_module()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/webhooks/cloudflare")
    return app, mod


def test_cloudy_endpoint_503_when_disabled(gated_app, monkeypatch):
    app, mod = gated_app
    # Force the system_config path to also report off.
    monkeypatch.setattr(mod, "cloudy_ingestion_enabled", lambda: False)
    client = TestClient(app)
    resp = client.post(
        "/api/webhooks/cloudflare/cloudy",
        content=json.dumps({"event_id": "x", "cloudy_summary": "hi"}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


def test_cloudy_health_reports_disabled_state(gated_app, monkeypatch):
    app, mod = gated_app
    monkeypatch.setattr(mod, "cloudy_ingestion_enabled", lambda: False)
    monkeypatch.setattr(mod, "_get_secret", lambda: None)
    client = TestClient(app)
    resp = client.get("/api/webhooks/cloudflare/cloudy/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["secret_configured"] is False
    assert body["receiver"] == "cloudflare-cloudy"
