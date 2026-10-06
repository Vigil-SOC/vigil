"""Tests for Cloudflare/Cloudforce One integration.

Covers:
- core/integrations/cloudflare/tool.py — the MCP server fails closed (no-op + clear error)
  when the integration is disabled, and the REST helpers shape arguments
  correctly when enabled (Cloudflare API itself is mocked).
- services/threat_feed_service.py — STIX 2.1 indicator parsing.
- core/integrations/cloudflare/cloudflare_webhooks_router.py — the Cloudy receiver returns 503
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# core/integrations/cloudflare/tool.py — REST helpers + disabled-integration behavior
# ---------------------------------------------------------------------------


def _import_cloudflare_tool():
    """Load core/integrations/cloudflare/tool.py by path (no package side effects)."""
    spec = importlib.util.spec_from_file_location(
        "cloudflare_tool_under_test",
        _REPO_ROOT / "core" / "integrations" / "cloudflare" / "tool.py",
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
    # The token comes from the secrets store via the descriptor-driven resolver,
    # not from get_integration_config — that store never holds a secret.
    with patch.object(cf, "is_integration_enabled", return_value=True), patch.object(
        cf, "resolve", return_value={"account_id": "abc", "api_token": None}
    ):
        assert cf._config() is None


def test_waf_block_ip_requires_account_id():
    cf = _import_cloudflare_tool()
    out = cf._waf_block_ip(api_token="t", account_id=None, ip="1.2.3.4", reason="test")
    assert out == {"error": "account_id required for WAF IP Access Rules"}


def test_waf_block_ip_posts_correct_payload():
    cf = _import_cloudflare_tool()
    fake = MagicMock()
    fake.status_code = 200
    fake.content = b"{}"
    fake.json.return_value = {"success": True, "result": {"id": "rule-1"}}

    with patch.object(cf.httpx, "post", return_value=fake) as posted:
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
    with patch.object(cf.httpx, "post", return_value=fake) as posted:
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
    from core.threat_intel.threat_feed_service import parse_stix_indicator

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
    from core.threat_intel.threat_feed_service import parse_stix_indicator

    obj = {
        "type": "indicator",
        "pattern": "[domain-name:value = 'a.example' OR domain-name:value = 'b.example']",
    }
    out = parse_stix_indicator(obj, source="cloudforce_one", collection_id=None)
    assert sorted(i.indicator_value for i in out) == ["a.example", "b.example"]
    assert all(i.indicator_type == "domain" for i in out)


def test_parse_stix_indicator_skips_non_indicator():
    from core.threat_intel.threat_feed_service import parse_stix_indicator

    assert (
        parse_stix_indicator({"type": "malware"}, source="x", collection_id=None) == []
    )


# ---------------------------------------------------------------------------
# core/integrations/cloudflare/cloudflare_webhooks_router.py — gating
# ---------------------------------------------------------------------------


def _load_webhook_module():
    spec = importlib.util.spec_from_file_location(
        "cloudflare_webhooks_under_test",
        _REPO_ROOT
        / "core"
        / "integrations"
        / "cloudflare"
        / "cloudflare_webhooks_router.py",
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


def _signed_post(client, body: bytes, secret: str, sig: str | None = None):
    import hashlib
    import hmac

    sig = (
        sig
        if sig is not None
        else hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    )
    headers = {"Content-Type": "application/json"}
    if sig:
        headers["X-Cloudflare-Signature"] = sig
    return client.post("/api/webhooks/cloudflare/cloudy", content=body, headers=headers)


def test_cloudy_rejections_are_logged_counted_and_distinguished(
    gated_app, monkeypatch, caplog
):
    import logging

    from core import webhook_rejections as wr

    wr._counts.clear()
    wr._log_state.clear()
    app, mod = gated_app
    monkeypatch.setattr(mod, "cloudy_ingestion_enabled", lambda: True)
    client = TestClient(app)
    caplog.set_level(logging.WARNING)

    def reasons():
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    # no secret configured
    monkeypatch.setattr(mod, "_get_secret", lambda: None)
    assert _signed_post(client, b"{}", "x", sig="s").status_code == 503
    assert "endpoint=cloudflare/cloudy reason=no_secret source_ip=" in reasons()[-1]

    # lookup failure is a 503 with its own detail (was an unhandled 500)
    def boom():
        raise RuntimeError("vault down")

    monkeypatch.setattr(mod, "_get_secret", boom)
    r = _signed_post(client, b"{}", "x", sig="s")
    assert r.status_code == 503
    assert "lookup failed" in r.json()["detail"]
    assert "reason=secret_lookup_failed" in reasons()[-1]

    # missing + bad signature; a burst of bad ones is one log line, exact count
    monkeypatch.setattr(mod, "_get_secret", lambda: "s3cret")
    assert _signed_post(client, b"{}", "s3cret", sig="").status_code == 401
    assert "reason=missing_signature" in reasons()[-1]
    before = len(reasons())
    for _ in range(100):
        assert _signed_post(client, b"{}", "s3cret", sig="bad").status_code == 401
    assert len(reasons()) == before + 1
    assert "reason=bad_signature" in reasons()[-1]

    # disabled
    monkeypatch.setattr(mod, "cloudy_ingestion_enabled", lambda: False)
    assert _signed_post(client, b"{}", "s3cret").status_code == 503
    assert "reason=disabled" in reasons()[-1]

    health = client.get("/api/webhooks/cloudflare/cloudy/health").json()
    assert health["rejections"]["cloudflare/cloudy"] == {
        "no_secret": 1,
        "secret_lookup_failed": 1,
        "missing_signature": 1,
        "bad_signature": 100,
        "disabled": 1,
    }
