"""Synthetic contract tests for the optional external visualization workflow."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.integrations.vstrike.client import VStrikeToolNotImplemented
from core.integrations.vstrike.events import project_faults
from services.api.routers import vstrike


def test_focus_is_a_request_and_never_claims_visual_success():
    service = MagicMock(has_ui_credentials=True)
    with patch.object(vstrike, "get_vstrike_service", return_value=service):
        response = vstrike.ui_find_by_ip_then_zoom(
            vstrike.VStrikeFocusRequest(
                network_id="example-network",
                ip4s=["192.0.2.2", "192.0.2.2", "198.51.100.10"],
            )
        )
    assert response == {
        "status": "requested",
        "network_id": "example-network",
        "ip4s": ["192.0.2.2", "198.51.100.10"],
    }
    service.ui_find_by_ip_then_zoom.assert_called_once_with(
        "example-network", response["ip4s"]
    )


@pytest.mark.parametrize(
    "addresses",
    [[], ["192.0.2.2/32"], ["192.0.2"], ["2001:db8::1"], ["192.0.2.2"] * 51],
)
def test_focus_rejects_invalid_and_unsupported_addresses(addresses):
    with pytest.raises(ValidationError):
        vstrike.VStrikeFocusRequest(network_id="example-network", ip4s=addresses)


def test_blank_network_is_rejected():
    with pytest.raises(ValidationError):
        vstrike.VStrikeFocusRequest(network_id="  ", ip4s=["192.0.2.2"])


@pytest.mark.parametrize(
    "error, status",
    [(RuntimeError("token=private"), 502), (VStrikeToolNotImplemented("private"), 501)],
)
def test_focus_errors_do_not_expose_provider_data(error, status):
    service = MagicMock(has_ui_credentials=True)
    service.ui_find_by_ip_then_zoom.side_effect = error
    with patch.object(
        vstrike, "get_vstrike_service", return_value=service
    ), pytest.raises(HTTPException) as caught:
        vstrike.ui_find_by_ip_then_zoom(
            vstrike.VStrikeFocusRequest(
                network_id="example-network", ip4s=["192.0.2.2"]
            )
        )
    assert caught.value.status_code == status
    assert "private" not in caught.value.detail


def test_events_preserve_unknown_time_zero_and_explicit_source_level():
    result = project_faults(
        [
            {
                "eventId": "one",
                "date": "2026-01-02T07:00:00-05:00",
                "level": "danger",
                "ip4s": ["192.0.2.2", "192.0.2.2", "not-an-ip"],
                "properties": {"rawMeasurement": {"value": 0, "secret": "excluded"}},
            },
            {"eventId": "one", "date": "2026-01-01T12:00:00Z"},
            {"eventId": "unknown", "date": "bad"},
            {"missing": "id"},
        ]
    )
    assert len(result) == 2
    assert result[0]["occurred_at"] == "2026-01-02T12:00:00+00:00"
    assert result[0]["ip4s"] == ["192.0.2.2"]
    assert result[0]["measurement"] == {"value": "0"}
    assert result[1]["occurred_at"] is None
    assert result[1]["source_level"] == ""
    assert all("severity" not in row and "score" not in row for row in result)


def test_event_read_is_bounded_and_does_not_call_storage():
    service = MagicMock(has_ui_credentials=True)
    service.storyline_faults_page.return_value = [{"eventId": "one"}]
    with patch.object(
        vstrike, "get_vstrike_service", return_value=service
    ), patch.object(vstrike, "data_service") as storage:
        result = vstrike.query_faults(
            vstrike.VStrikeFaultQueryRequest(storyline_id="example-scenario", limit=1)
        )
    assert result["truncated"] is True
    assert result["limit"] == 1
    assert storage.mock_calls == []
    service.storyline_faults_page.assert_called_once_with("example-scenario", 1)
    with pytest.raises(ValidationError):
        vstrike.VStrikeFaultQueryRequest(storyline_id="example-scenario", limit=251)


def test_new_routes_require_auth_before_contacting_vstrike(monkeypatch):
    from services.api.middleware import auth

    monkeypatch.setattr(auth, "DEV_MODE", False)
    app = FastAPI()
    app.include_router(vstrike.authenticated_router, prefix="/api/integrations/vstrike")
    with patch.object(vstrike, "get_vstrike_service") as service:
        client = TestClient(app)
        for path, payload in [
            (
                "ui/find-by-ip-then-zoom",
                {"network_id": "example-network", "ip4s": ["192.0.2.2"]},
            ),
            ("faults/query", {"storyline_id": "example-scenario"}),
        ]:
            assert (
                client.post(
                    f"/api/integrations/vstrike/{path}", json=payload
                ).status_code
                == 401
            )
    service.assert_not_called()
