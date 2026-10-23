"""Saving General settings keeps the demo_mode flag that shares its file."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core.config import _load_json_config, is_demo_mode, vigil_path
from services.api.middleware.auth import get_current_active_user

pytestmark = pytest.mark.unit

_GENERAL = {
    "auto_start_sync": True,
    "show_notifications": False,
    "theme": "light",
    "enable_keyring": True,
}


@pytest.fixture
def client(authenticate_app, monkeypatch, tmp_path):
    from services.api.routers import config as config_module

    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    app = FastAPI()
    app.include_router(
        config_module.router,
        prefix="/api/config",
        dependencies=[Depends(get_current_active_user)],
    )
    authenticate_app(app)
    with (
        patch.object(config_module, "get_config_service", return_value=MagicMock()),
        patch.object(config_module, "get_secrets_manager"),
    ):
        yield TestClient(app)


@pytest.mark.parametrize("demo", [True, False])
def test_general_save_keeps_demo_mode(client, demo):
    assert (
        client.post("/api/config/demo-mode", json={"enabled": demo}).status_code == 200
    )

    saved = client.post("/api/config/general", json=_GENERAL)

    assert saved.status_code == 200, saved.text
    assert is_demo_mode() is demo
    assert client.get("/api/config/demo-mode").json()["enabled"] is demo
    assert _load_json_config(vigil_path("general_config.json"))["demo_mode"] is demo


@pytest.mark.parametrize("content", [b"[]", b"null", b'"x"', b"\xff\xfe{"])
def test_general_save_survives_unusable_config_file(client, content):
    vigil_path("general_config.json", write=True).write_bytes(content)

    saved = client.post("/api/config/general", json=_GENERAL)

    assert saved.status_code == 200, saved.text
    assert _load_json_config(vigil_path("general_config.json")) == _GENERAL


def test_demo_mode_save_keeps_general_settings(client):
    client.post("/api/config/general", json=_GENERAL)

    client.post("/api/config/demo-mode", json={"enabled": True})
    client.post("/api/config/general", json={**_GENERAL, "theme": "dark"})

    assert _load_json_config(vigil_path("general_config.json")) == {
        **_GENERAL,
        "theme": "dark",
        "demo_mode": True,
    }
