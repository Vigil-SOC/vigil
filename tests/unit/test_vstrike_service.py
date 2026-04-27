"""Unit tests for services/vstrike_service.py (mocked HTTP)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.vstrike_service import (  # noqa: E402
    VStrikeService,
    _jwt_cache,
    get_vstrike_service,
)


@pytest.fixture(autouse=True)
def _clear_jwt_cache():
    """Module-level JWT cache must not leak across tests."""
    _jwt_cache.clear()
    yield
    _jwt_cache.clear()


@pytest.fixture
def isolate_secrets(monkeypatch):
    """Force the factory's `get_secret` lookups to consult only os.environ.

    The real secrets manager reads from encrypted store + env + dotenv +
    keyring. On a developer machine that store may already contain
    VSTRIKE_* values (e.g. saved via a live Settings UI test), which would
    leak into tests that expect those keys to be unset. This fixture
    patches the factory's get_secret import to a thin wrapper that only
    looks at os.environ — which the test then controls via monkeypatch.
    """
    import backend.secrets_manager as sm

    def _env_only(key, default=None):
        return os.environ.get(key, default)

    monkeypatch.setattr(sm, "get_secret", _env_only)
    return monkeypatch


def _service(**kwargs) -> VStrikeService:
    return VStrikeService(
        base_url=kwargs.get("base_url", "https://vstrike.example.com"),
        api_key=kwargs.get("api_key", "test-key"),
        verify_ssl=kwargs.get("verify_ssl", False),
        username=kwargs.get("username"),
        password=kwargs.get("password"),
    )


def _ui_service(**kwargs) -> VStrikeService:
    """Build a service configured for the UI/MCP path (no api_key)."""
    return VStrikeService(
        base_url=kwargs.get("base_url", "https://vstrike.example.com"),
        api_key=kwargs.get("api_key"),
        verify_ssl=False,
        username=kwargs.get("username", "deeptempo_manager"),
        password=kwargs.get("password", "secret"),
    )


def _mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


def test_sets_bearer_auth_header():
    svc = _service(api_key="abc123")
    assert svc.session.headers["Authorization"] == "Bearer abc123"


def test_test_connection_success():
    svc = _service()
    with patch.object(svc.session, "get", return_value=_mock_response(200)):
        ok, msg = svc.test_connection()
    assert ok is True
    assert "success" in msg.lower()


def test_test_connection_http_error():
    svc = _service()
    with patch.object(
        svc.session, "get", return_value=_mock_response(503, text="down")
    ):
        ok, msg = svc.test_connection()
    assert ok is False
    assert "503" in msg


def test_test_connection_network_error():
    import requests

    svc = _service()
    with patch.object(
        svc.session,
        "get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        ok, msg = svc.test_connection()
    assert ok is False
    assert "Connection error" in msg


def test_get_asset_topology_returns_body_on_200():
    svc = _service()
    body = {"asset_id": "srv-01", "segment": "vlan-10"}
    with patch.object(
        svc.session, "get", return_value=_mock_response(200, json_body=body)
    ):
        result = svc.get_asset_topology("srv-01")
    assert result == body


def test_get_asset_topology_returns_none_on_error():
    svc = _service()
    with patch.object(svc.session, "get", return_value=_mock_response(404)):
        assert svc.get_asset_topology("srv-missing") is None


def test_list_adjacent_returns_list():
    svc = _service()
    body = {"adjacent": [{"asset_id": "dc-01", "hop_distance": 1}]}
    with patch.object(
        svc.session, "get", return_value=_mock_response(200, json_body=body)
    ):
        adjacent = svc.list_adjacent("srv-01")
    assert adjacent == [{"asset_id": "dc-01", "hop_distance": 1}]


def test_find_findings_by_segment_uses_query_params():
    svc = _service()
    body = {"findings": [{"finding_id": "f1"}]}
    with patch.object(
        svc.session, "get", return_value=_mock_response(200, json_body=body)
    ) as mock_get:
        result = svc.find_findings_by_segment("dmz", limit=50)

    assert result == [{"finding_id": "f1"}]
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"] == {"segment": "dmz", "limit": 50}


def test_get_vstrike_service_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("VSTRIKE_BASE_URL", raising=False)
    monkeypatch.delenv("VSTRIKE_API_KEY", raising=False)
    with patch(
        "core.config.get_integration_config",
        return_value={},
    ):
        assert get_vstrike_service() is None


def test_get_vstrike_service_from_env(monkeypatch):
    monkeypatch.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    monkeypatch.setenv("VSTRIKE_API_KEY", "env-key")
    svc = get_vstrike_service()
    assert svc is not None
    assert svc.base_url == "https://vstrike.example.com"
    assert svc.api_key == "env-key"


def test_get_vstrike_service_falls_back_to_integration_config(monkeypatch):
    monkeypatch.delenv("VSTRIKE_BASE_URL", raising=False)
    monkeypatch.delenv("VSTRIKE_API_KEY", raising=False)
    monkeypatch.delenv("VSTRIKE_USERNAME", raising=False)
    monkeypatch.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch(
        "core.config.get_integration_config",
        return_value={
            "url": "https://cfg.example.com",
            "api_key": "cfg-key",
            "verify_ssl": False,
        },
    ):
        svc = get_vstrike_service()
    assert svc is not None
    assert svc.base_url == "https://cfg.example.com"
    assert svc.api_key == "cfg-key"
    assert svc.verify_ssl is False


# ---------------------------------------------------------------------------
# Credential predicates + factory resolution for UI path
# ---------------------------------------------------------------------------


def test_has_ui_credentials_only_true_with_both():
    assert _service(username="u").has_ui_credentials is False
    assert _service(password="p").has_ui_credentials is False
    assert _service(username="u", password="p").has_ui_credentials is True


def test_has_api_credentials_tracks_api_key():
    assert _service(api_key="k").has_api_credentials is True
    assert _service(api_key=None).has_api_credentials is False


def test_get_vstrike_service_with_only_ui_credentials(monkeypatch):
    monkeypatch.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    monkeypatch.delenv("VSTRIKE_API_KEY", raising=False)
    monkeypatch.setenv("VSTRIKE_USERNAME", "deeptempo_manager")
    monkeypatch.setenv("VSTRIKE_PASSWORD", "secret")
    with patch("core.config.get_integration_config", return_value={}):
        svc = get_vstrike_service()
    assert svc is not None
    assert svc.has_api_credentials is False
    assert svc.has_ui_credentials is True


def test_get_vstrike_service_returns_none_with_only_username(isolate_secrets):
    isolate_secrets.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    isolate_secrets.delenv("VSTRIKE_API_KEY", raising=False)
    isolate_secrets.setenv("VSTRIKE_USERNAME", "deeptempo_manager")
    isolate_secrets.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch("core.config.get_integration_config", return_value={}):
        svc = get_vstrike_service()
    # Username alone is not enough.
    assert svc is None


def test_get_vstrike_service_ui_credentials_from_integration_config(
    isolate_secrets,
):
    isolate_secrets.delenv("VSTRIKE_BASE_URL", raising=False)
    isolate_secrets.delenv("VSTRIKE_API_KEY", raising=False)
    isolate_secrets.delenv("VSTRIKE_USERNAME", raising=False)
    isolate_secrets.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch(
        "core.config.get_integration_config",
        return_value={
            "url": "https://cfg.example.com",
            "username": "alice",
            "password": "wonderland",
        },
    ):
        svc = get_vstrike_service()
    assert svc is not None
    assert svc.username == "alice"
    assert svc.password == "wonderland"
    assert svc.has_ui_credentials is True


# ---------------------------------------------------------------------------
# MCP login + JWT cache
# ---------------------------------------------------------------------------


def test_mcp_login_caches_jwt_across_calls():
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"token": "jwt-1"}),
    ) as mock_post:
        token1 = svc._ensure_jwt()
        token2 = svc._ensure_jwt()
    assert token1 == "jwt-1"
    assert token2 == "jwt-1"
    # Cache prevents the second call from re-logging-in.
    assert mock_post.call_count == 1


def test_mcp_login_extracts_alternate_token_keys():
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"jwt": "from-jwt-key"}),
    ):
        assert svc._ensure_jwt() == "from-jwt-key"


def test_mcp_login_raises_on_http_error():
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(401, text="bad creds"),
    ):
        with pytest.raises(RuntimeError, match="mcp-login HTTP 401"):
            svc._ensure_jwt()


def test_mcp_login_raises_when_response_missing_token():
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"unexpected": "shape"}),
    ):
        with pytest.raises(RuntimeError, match="missing token"):
            svc._ensure_jwt()


def test_mcp_login_requires_credentials():
    svc = _service(api_key="only-key")  # no username/password
    with pytest.raises(RuntimeError, match="not configured"):
        svc._ensure_jwt()


# ---------------------------------------------------------------------------
# MCP tool calls + 401 retry
# ---------------------------------------------------------------------------


def test_call_mcp_tool_re_logs_in_on_401():
    svc = _ui_service()
    refreshed_login = _mock_response(200, json_body={"token": "jwt-B"})
    unauthorized = _mock_response(401, text="expired")
    success = _mock_response(
        200,
        json_body={"result": {"token": "ui-token-xyz"}},
    )

    # Pre-seed the cache so the first tool call uses jwt-A immediately
    # without hitting /mcp-login, then expect: 401 → invalidate →
    # re-login → success.
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)

    sequence = [unauthorized, refreshed_login, success]

    def consume(*_args, **_kwargs):
        return sequence.pop(0)

    with patch("services.vstrike_service.requests.post", side_effect=consume):
        token = svc.get_ui_login_token()

    assert token == "ui-token-xyz"
    cached_token, _ = _jwt_cache[(svc.base_url, svc.username)]
    assert cached_token == "jwt-B"
    assert sequence == []


def test_call_mcp_tool_propagates_jsonrpc_error():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    error_resp = _mock_response(
        200,
        json_body={"error": {"code": -32601, "message": "Tool not found"}},
    )
    with patch(
        "services.vstrike_service.requests.post",
        return_value=error_resp,
    ):
        with pytest.raises(RuntimeError, match="Tool not found"):
            svc.get_ui_login_token()


def test_list_networks_unwraps_mcp_content_envelope():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    # MCP standard content-list envelope where text is JSON.
    body = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": '{"networks": [{"id": "n-1", "name": "Prod"}]}',
                }
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ):
        networks = svc.list_networks()
    assert networks == [{"id": "n-1", "name": "Prod"}]


def test_load_network_in_ui_passes_network_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.load_network_in_ui("net-42")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "ui-network-load"
    assert payload["params"]["arguments"] == {"networkId": "net-42"}


def test_iframe_url_embeds_token():
    svc = _ui_service(base_url="https://vstrike.net")
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(
            200, json_body={"result": {"token": "short-lived-abc"}}
        ),
    ):
        url = svc.iframe_url()
    assert url == "https://vstrike.net/login?token=short-lived-abc"


# ---------------------------------------------------------------------------
# Wire-format regressions discovered against the live VStrike server
# ---------------------------------------------------------------------------


def _sse_response(json_body):
    """Build a mock response that mimics VStrike's SSE framing."""
    import json as _json

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "text/event-stream"}
    resp.text = "event: message\ndata: " + _json.dumps(json_body) + "\n\n"
    # Calling `.json()` on an SSE body would raise; ensure tests fail loudly
    # if the parser ever reaches for it.
    resp.json.side_effect = ValueError("SSE body is not JSON-parseable")
    return resp


def test_mcp_login_extracts_jsonwebtoken_field():
    """VStrike's /mcp-login returns the JWT under the `jsonwebtoken` key."""
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(
            200, json_body={"jsonwebtoken": "eyJ...real.jwt..."}
        ),
    ):
        assert svc._ensure_jwt() == "eyJ...real.jwt..."


def test_mcp_login_handles_sse_response():
    """Even /mcp-login may come back as text/event-stream."""
    svc = _ui_service()
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_sse_response({"jsonwebtoken": "sse-jwt"}),
    ):
        assert svc._ensure_jwt() == "sse-jwt"


def test_call_mcp_tool_parses_sse_with_structured_content():
    """ui-login-token returns SSE-framed JSON with token in structuredContent."""
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [],
            "structuredContent": {
                "token": "DV3VK7JWZG",
                "loginUrl": "https://vstrike.net:443/login?token=DV3VK7JWZG",
            },
            "isError": False,
        },
        "jsonrpc": "2.0",
        "id": 2,
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_sse_response(body),
    ):
        token = svc.get_ui_login_token()
    assert token == "DV3VK7JWZG"


def test_list_networks_parses_sse_with_structured_content():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [],
            "structuredContent": {
                "networks": [
                    {"label": "Test", "networkId": "69e5332fd395368a5f18f3f1"},
                    {
                        "label": "DeepTempo AI SoC Demo",
                        "networkId": "69e3e13ab79027157149e32d",
                    },
                ],
                "count": 2,
            },
            "isError": False,
        },
        "jsonrpc": "2.0",
        "id": 3,
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_sse_response(body),
    ):
        networks = svc.list_networks()
    assert len(networks) == 2
    assert networks[0]["networkId"] == "69e5332fd395368a5f18f3f1"
    assert networks[1]["label"] == "DeepTempo AI SoC Demo"


def test_call_mcp_tool_raises_on_tool_isError():
    """A tool result with isError=true is surfaced as a RuntimeError."""
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [{"type": "text", "text": "Network not found"}],
            "isError": True,
        },
        "jsonrpc": "2.0",
        "id": 1,
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_sse_response(body),
    ):
        with pytest.raises(RuntimeError, match="tool error"):
            svc.load_network_in_ui("does-not-exist")
