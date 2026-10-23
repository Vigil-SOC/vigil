"""``/api/custom-integrations`` answers each failure with its own status.

The service raises typed errors and the app's exception handlers pick the
status: a bad id is 400, a missing integration is 404, a disk failure is a 500
that does not quote the OS error.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.integrations.custom_integration_service import CustomIntegrationService
from services.api.errors import register_exception_handlers
from services.api.middleware.auth import get_current_active_user
from services.api.routers import custom_integrations as ci

pytestmark = pytest.mark.unit

GOOD = (
    'from mcp.server import Server\nServer("x")\n'
    "def on_list_tools(): pass\ndef on_call_tool(): pass\n"
    "async def main():\n    pass\n"
)
DISK_ERROR = PermissionError(13, "Permission denied", "metadata.json")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "require_integrations_admin", lambda user: None)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(ci.router, prefix="/api/custom-integrations")
    app.dependency_overrides[get_current_active_user] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _save(client, integration_id="acme", server_code=GOOD):
    return client.post(
        "/api/custom-integrations/save",
        json={
            "integration_id": integration_id,
            "metadata": {},
            "server_code": server_code,
        },
    )


def test_validate_saved_integration_reports_checks(client):
    assert _save(client).status_code == 200
    r = client.post("/api/custom-integrations/acme/validate")
    assert r.status_code == 200
    assert r.json()["valid"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/custom-integrations/BAD..ID/validate"),
        ("delete", "/api/custom-integrations/BAD..ID"),
    ],
)
def test_bad_id_is_400(client, method, path):
    assert getattr(client, method)(path).status_code == 400


@pytest.mark.parametrize("integration_id", ["../x", "BAD..ID"])
def test_save_bad_id_is_400_and_writes_nothing(client, tmp_path, integration_id):
    assert _save(client, integration_id).status_code == 400
    assert not (tmp_path / "custom_integrations" / "metadata.json").exists()


def test_save_oversize_code_is_400(client):
    r = _save(client, "huge", "a" * (300 * 1024))
    assert r.status_code == 400
    assert "byte limit" in r.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/custom-integrations/acme/validate"),
        ("delete", "/api/custom-integrations/acme"),
        ("delete", "/api/custom-integrations/integration_id"),
    ],
)
def test_missing_integration_is_404(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_delete_saved_integration(client):
    assert _save(client).status_code == 200
    assert client.delete("/api/custom-integrations/acme").status_code == 200
    assert client.delete("/api/custom-integrations/acme").status_code == 404


@pytest.mark.parametrize("action", ["save", "delete"])
def test_disk_error_is_500_without_os_text(client, action):
    assert _save(client).status_code == 200
    with patch.object(
        CustomIntegrationService, "_save_metadata", side_effect=DISK_ERROR
    ):
        r = (
            _save(client)
            if action == "save"
            else client.delete("/api/custom-integrations/acme")
        )
    assert r.status_code == 500
    assert "Permission denied" not in r.text
