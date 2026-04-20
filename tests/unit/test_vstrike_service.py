"""Unit tests for services/vstrike_service.py (mocked HTTP)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.vstrike_service import (  # noqa: E402
    VStrikeService,
    get_vstrike_service,
)


def _service(**kwargs) -> VStrikeService:
    return VStrikeService(
        base_url=kwargs.get("base_url", "https://vstrike.example.com"),
        api_key=kwargs.get("api_key", "test-key"),
        verify_ssl=kwargs.get("verify_ssl", False),
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
