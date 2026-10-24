from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import config as config_router

pytestmark = pytest.mark.unit

GENERAL_DEFAULTS = {
    "show_notifications": True,
    "enable_keyring": False,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(config_router.router, prefix="/api/config")
    return TestClient(app)


def _service(**rows):
    svc = MagicMock()
    svc.read_system_config.side_effect = rows.get
    return svc


@pytest.mark.parametrize("path", ["/api/config/general", "/api/config/theme"])
def test_a_failed_read_is_503_not_defaults(client, path):
    with patch(
        "core.storage.config_service.get_session",
        side_effect=RuntimeError("db down"),
    ):
        resp = client.get(path)

    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("path", "key", "stored"),
    [
        (
            "/api/config/general",
            "general.settings",
            {**GENERAL_DEFAULTS, "show_notifications": False},
        ),
        ("/api/config/theme", "theme.current", {"theme": "light"}),
    ],
)
def test_the_stored_row_is_returned(client, path, key, stored):
    with patch(
        "services.api.routers.config.get_config_service",
        return_value=_service(**{key: stored}),
    ):
        resp = client.get(path)

    assert resp.status_code == 200
    assert resp.json() == stored


def test_missing_rows_read_as_defaults_and_ignore_state_files(client, tmp_path):
    (tmp_path / "theme_config.json").write_text('{"theme": "light"}')
    (tmp_path / "general_config.json").write_text('{"show_notifications": false}')
    with patch(
        "services.api.routers.config.get_config_service", return_value=_service()
    ):
        general = client.get("/api/config/general")
        theme = client.get("/api/config/theme")

    assert general.json() == GENERAL_DEFAULTS
    assert theme.json() == {"theme": "dark"}
