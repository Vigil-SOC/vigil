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
    """Build a fully-credentialed service. JWT-only auth — no api_key."""
    return VStrikeService(
        base_url=kwargs.get("base_url", "https://vstrike.example.com"),
        verify_ssl=kwargs.get("verify_ssl", False),
        username=kwargs.get("username", "deeptempo_manager"),
        password=kwargs.get("password", "secret"),
    )


# Back-compat alias for tests that historically distinguished the two.
def _ui_service(**kwargs) -> VStrikeService:
    return _service(**kwargs)


def _seed_jwt(svc: VStrikeService, value: str = "jwt-cached") -> None:
    """Pre-seed the module-level JWT cache so REST tests skip /mcp-login."""
    _jwt_cache[(svc.base_url, svc.username)] = (value, 9_999_999_999.0)


def _mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    resp.headers = {"Content-Type": "application/json"}
    return resp


def test_test_connection_success():
    svc = _service()
    _seed_jwt(svc)
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(200),
    ):
        ok, msg = svc.test_connection()
    assert ok is True
    assert "success" in msg.lower()


def test_test_connection_http_error():
    svc = _service()
    _seed_jwt(svc)
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(503, text="down"),
    ):
        ok, msg = svc.test_connection()
    assert ok is False
    assert "503" in msg


def test_test_connection_network_error():
    import requests

    svc = _service()
    _seed_jwt(svc)
    with patch(
        "services.vstrike_service.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        ok, msg = svc.test_connection()
    assert ok is False
    assert "Connection error" in msg


def test_get_asset_topology_returns_body_on_200():
    svc = _service()
    _seed_jwt(svc)
    body = {"asset_id": "srv-01", "segment": "vlan-10"}
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(200, json_body=body),
    ):
        result = svc.get_asset_topology("srv-01")
    assert result == body


def test_get_asset_topology_attaches_jwt_bearer():
    """Every legacy REST call must inject Authorization: Bearer <jwt>."""
    svc = _service()
    _seed_jwt(svc, "jwt-from-cache")
    body = {"asset_id": "srv-01"}
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(200, json_body=body),
    ) as mock_get:
        svc.get_asset_topology("srv-01")
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer jwt-from-cache"


def test_get_asset_topology_returns_none_on_error():
    svc = _service()
    _seed_jwt(svc)
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(404),
    ):
        assert svc.get_asset_topology("srv-missing") is None


def test_legacy_rest_retries_on_401_after_invalidating_jwt():
    """A 401 on the legacy REST path must drop the cached JWT, re-login,
    and retry the request once — same pattern as `_call_mcp_tool`."""
    svc = _service()
    _seed_jwt(svc, "jwt-stale")

    refreshed_login = _mock_response(200, json_body={"jsonwebtoken": "jwt-fresh"})
    unauthorized = _mock_response(401, text="expired")
    body = {"asset_id": "srv-01", "segment": "core"}
    success = _mock_response(200, json_body=body)

    with patch(
        "services.vstrike_service.requests.get",
        side_effect=[unauthorized, success],
    ) as mock_get, patch(
        "services.vstrike_service.requests.post",
        return_value=refreshed_login,
    ) as mock_post:
        result = svc.get_asset_topology("srv-01")

    assert result == body
    # Two GETs: first 401 (jwt-stale), second 200 (jwt-fresh).
    assert mock_get.call_count == 2
    # One POST to /mcp-login between them.
    assert mock_post.call_count == 1
    # Cache now holds the refreshed JWT.
    assert _jwt_cache[(svc.base_url, svc.username)][0] == "jwt-fresh"


def test_list_adjacent_returns_list():
    svc = _service()
    _seed_jwt(svc)
    body = {"adjacent": [{"asset_id": "dc-01", "hop_distance": 1}]}
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(200, json_body=body),
    ):
        adjacent = svc.list_adjacent("srv-01")
    assert adjacent == [{"asset_id": "dc-01", "hop_distance": 1}]


def test_find_findings_by_segment_uses_query_params():
    svc = _service()
    _seed_jwt(svc)
    body = {"findings": [{"finding_id": "f1"}]}
    with patch(
        "services.vstrike_service.requests.get",
        return_value=_mock_response(200, json_body=body),
    ) as mock_get:
        result = svc.find_findings_by_segment("dmz", limit=50)

    assert result == [{"finding_id": "f1"}]
    assert mock_get.call_args.kwargs["params"] == {"segment": "dmz", "limit": 50}


def test_get_vstrike_service_returns_none_when_unconfigured(isolate_secrets):
    isolate_secrets.delenv("VSTRIKE_BASE_URL", raising=False)
    isolate_secrets.delenv("VSTRIKE_USERNAME", raising=False)
    isolate_secrets.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch("core.config.get_integration_config", return_value={}):
        assert get_vstrike_service() is None


def test_get_vstrike_service_from_env(isolate_secrets):
    isolate_secrets.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    isolate_secrets.setenv("VSTRIKE_USERNAME", "alice")
    isolate_secrets.setenv("VSTRIKE_PASSWORD", "wonderland")
    svc = get_vstrike_service()
    assert svc is not None
    assert svc.base_url == "https://vstrike.example.com"
    assert svc.username == "alice"
    assert svc.password == "wonderland"


def test_get_vstrike_service_falls_back_to_integration_config(isolate_secrets):
    isolate_secrets.delenv("VSTRIKE_BASE_URL", raising=False)
    isolate_secrets.delenv("VSTRIKE_USERNAME", raising=False)
    isolate_secrets.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch(
        "core.config.get_integration_config",
        return_value={
            "url": "https://cfg.example.com",
            "username": "alice",
            "password": "wonderland",
            "verify_ssl": False,
        },
    ):
        svc = get_vstrike_service()
    assert svc is not None
    assert svc.base_url == "https://cfg.example.com"
    assert svc.username == "alice"
    assert svc.password == "wonderland"
    assert svc.verify_ssl is False


def test_get_vstrike_service_ignores_legacy_api_key(isolate_secrets):
    """Older installs may still have VSTRIKE_API_KEY in the secrets store —
    the factory tolerates it but does not use it."""
    isolate_secrets.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    isolate_secrets.setenv("VSTRIKE_API_KEY", "leftover-from-old-install")
    isolate_secrets.delenv("VSTRIKE_USERNAME", raising=False)
    isolate_secrets.delenv("VSTRIKE_PASSWORD", raising=False)
    with patch("core.config.get_integration_config", return_value={}):
        svc = get_vstrike_service()
    # Without username+password, an api_key alone is no longer enough.
    assert svc is None


# ---------------------------------------------------------------------------
# Credential predicates
# ---------------------------------------------------------------------------


def test_has_ui_credentials_only_true_with_both():
    assert _service(username="u", password=None).has_ui_credentials is False
    assert _service(username=None, password="p").has_ui_credentials is False
    assert _service(username="u", password="p").has_ui_credentials is True


def test_has_api_credentials_is_alias_for_has_ui_credentials():
    """JWT-only auth: 'configured' means username+password, regardless of which
    predicate name a caller queries."""
    svc = _service()
    assert svc.has_api_credentials is svc.has_ui_credentials is True
    assert _service(username=None, password=None).has_api_credentials is False


def test_get_vstrike_service_with_only_ui_credentials(isolate_secrets):
    isolate_secrets.setenv("VSTRIKE_BASE_URL", "https://vstrike.example.com")
    isolate_secrets.setenv("VSTRIKE_USERNAME", "deeptempo_manager")
    isolate_secrets.setenv("VSTRIKE_PASSWORD", "secret")
    with patch("core.config.get_integration_config", return_value={}):
        svc = get_vstrike_service()
    assert svc is not None
    assert svc.has_ui_credentials is True
    assert svc.has_api_credentials is True


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
    """Without username + password, _ensure_jwt must refuse, not blank-login."""
    svc = _service(username=None, password=None)
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


def test_killchain_replay_in_ui_passes_full_payload():
    from services.vstrike_service import VStrikeToolNotImplemented  # noqa: F401

    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    steps = [
        {"node_id": "asset-1", "timestamp": "2026-04-28T11:00:00Z"},
        {
            "node_id": "asset-2",
            "timestamp": "2026-04-28T11:05:00Z",
            "technique": "T1021.002",
        },
    ]
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(
            200, json_body={"result": {"content": [{"type": "text", "text": "queued"}]}}
        ),
    ) as mock_post:
        svc.killchain_replay_in_ui("net-7", steps, loop=True, auto_play=False)

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-killchain-replay"
    args = payload["params"]["arguments"]
    assert args["networkId"] == "net-7"
    assert args["steps"] == steps
    assert args["loop"] is True
    assert args["auto_play"] is False


@pytest.mark.parametrize(
    "error_text",
    [
        "VStrike MCP ui-killchain-replay error: -32601 method not found",
        "VStrike MCP ui-killchain-replay error: tool not found",
        "VStrike MCP ui-killchain-replay error: unknown tool 'ui-killchain-replay'",
    ],
)
def test_killchain_replay_in_ui_raises_tool_not_implemented(error_text):
    """Engineer-side absence of the tool is converted to VStrikeToolNotImplemented."""
    from services.vstrike_service import VStrikeToolNotImplemented

    svc = _ui_service()
    with patch.object(svc, "_call_mcp_tool", side_effect=RuntimeError(error_text)):
        with pytest.raises(VStrikeToolNotImplemented):
            svc.killchain_replay_in_ui("net-1", [{"node_id": "a", "timestamp": "t"}])


def test_killchain_replay_in_ui_propagates_other_runtime_errors():
    """Transport / unrelated errors surface unchanged for the caller to map."""
    svc = _ui_service()
    with patch.object(
        svc, "_call_mcp_tool", side_effect=RuntimeError("connection refused")
    ):
        with pytest.raises(RuntimeError) as exc_info:
            svc.killchain_replay_in_ui("net-1", [{"node_id": "a", "timestamp": "t"}])
        assert "connection refused" in str(exc_info.value)


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
# New data-plane MCP tools (node search, drift, storylines, legends)
# ---------------------------------------------------------------------------


def test_node_search_passes_query_and_network_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"nodes": [{"node_id": "n1", "name": "Router-A"}]}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ) as mock_post:
        result = svc.node_search("router", network_id="net-1", limit=10)

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "node-search"
    args = payload["params"]["arguments"]
    assert args["query"] == "router"
    assert args["networkId"] == "net-1"
    assert args["limit"] == 10
    assert result == [{"node_id": "n1", "name": "Router-A"}]


def test_node_search_returns_none_on_error():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(500, text="boom"),
    ):
        assert svc.node_search("x") is None


def test_node_drift_get_passes_node_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"drift": [{"timestamp": "t1", "source": "cve"}]}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ) as mock_post:
        result = svc.node_drift_get("node-1", network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "node-drift-get"
    assert payload["params"]["arguments"]["nodeId"] == "node-1"
    assert payload["params"]["arguments"]["networkId"] == "net-1"
    assert result == [{"timestamp": "t1", "source": "cve"}]


def test_storyline_list_returns_storylines():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"storylines": [{"storyline_id": "s1", "name": "Exfil"}]}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ):
        result = svc.storyline_list(network_id="net-1")
    assert result == [{"storyline_id": "s1", "name": "Exfil"}]


def test_storyline_events_get_passes_storyline_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"events": [{"event_id": "e1", "timestamp": "t1"}]}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ) as mock_post:
        result = svc.storyline_events_get("s1", network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "storyline-events-get"
    assert payload["params"]["arguments"]["storylineId"] == "s1"
    assert result == [{"event_id": "e1", "timestamp": "t1"}]


def test_legend_run_list_returns_runs():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"legendRuns": [{"legend_run_id": "lr1", "name": "CVE-2026-001"}]}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ):
        result = svc.legend_run_list(network_id="net-1")
    assert result == [{"legend_run_id": "lr1", "name": "CVE-2026-001"}]


def test_legend_run_results_get_returns_dict():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    body = {
        "result": {
            "content": [
                {"type": "text", "text": '{"legend_run_id": "lr1", "results": {"critical": 3}}'}
            ]
        }
    }
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body=body),
    ) as mock_post:
        result = svc.legend_run_results_get("lr1", network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "legend-run-results-get"
    assert payload["params"]["arguments"]["legendRunId"] == "lr1"
    assert result == {"legend_run_id": "lr1", "results": {"critical": 3}}


# ---------------------------------------------------------------------------
# New UI control-plane MCP tools (camera, storyline, VCR)
# ---------------------------------------------------------------------------


def test_ui_camera_node_passes_node_ids():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_camera_node(["n1", "n2"], network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-camera-node"
    args = payload["params"]["arguments"]
    assert args["nodeIds"] == ["n1", "n2"]
    assert args["networkId"] == "net-1"


def test_ui_camera_position_passes_position_and_rotation():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_camera_position(
            {"x": 1.0, "y": 2.0, "z": 3.0},
            {"pitch": 0.5, "yaw": 1.0},
            network_id="net-1",
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-camera-position"
    args = payload["params"]["arguments"]
    assert args["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert args["rotation"] == {"pitch": 0.5, "yaw": 1.0}
    assert args["networkId"] == "net-1"


def test_ui_storyline_apply_passes_storyline_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_storyline_apply("s1", network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-storyline-apply"
    assert payload["params"]["arguments"]["storylineId"] == "s1"


def test_ui_storyline_mode_passes_mode():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_storyline_mode("replay", network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-storyline-mode"
    assert payload["params"]["arguments"]["mode"] == "replay"


def test_ui_storyline_forward_passes_network_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_storyline_forward(network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-storyline-forward"
    assert payload["params"]["arguments"]["networkId"] == "net-1"


def test_ui_storyline_backward_passes_network_id():
    svc = _ui_service()
    _jwt_cache[(svc.base_url, svc.username)] = ("jwt-A", 9_999_999_999.0)
    with patch(
        "services.vstrike_service.requests.post",
        return_value=_mock_response(200, json_body={"result": {"ok": True}}),
    ) as mock_post:
        svc.ui_storyline_backward(network_id="net-1")

    payload = mock_post.call_args.kwargs["json"]
    assert payload["params"]["name"] == "ui-storyline-backward"
    assert payload["params"]["arguments"]["networkId"] == "net-1"


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
