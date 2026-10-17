"""Switching demo mode applies to services already built, with no restart.

Most callers hold a ``DatabaseDataService`` built at import, so the switch has to
reach them through the saved setting, not through a new instance. The setting is
a database row, so every process and replica reads the same answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.api.v1.findings_router as v1_findings
import services.api.routers.config as config_router
from core.auth.current_user import get_current_user
from core.config import DEMO_MODE_CONFIG_KEY, is_demo_mode
from core.platform.demo_data_service import DemoDataService
from core.storage.database_data_service import DatabaseDataService

# The app is imported here, not under the fixtures below: a module imported while
# get_config_service is patched would keep the fake for the rest of the session.
from services.api.main import app
from services.api.routers.config import DemoModeConfig, GeneralConfig, set_demo_mode

pytestmark = pytest.mark.unit

USER = SimpleNamespace(user_id="u-test", username="test", is_active=True)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    return tmp_path


@pytest.fixture
def database(monkeypatch):
    """system_config as a dict, shared by every config service instance."""
    rows: dict = {}

    class _Config:
        def __init__(self, user_id: str = "system"):
            pass

        def read_system_config(self, key):
            return rows.get(key)

        def set_system_config(self, key, value, **_):
            rows[key] = value
            return True

    monkeypatch.setattr("core.storage.config_service.get_config_service", _Config)
    monkeypatch.setattr(config_router, "get_config_service", _Config)
    return rows


@pytest.fixture
def fresh_estate():
    saved = (
        DemoDataService._instance,
        DemoDataService._findings,
        DemoDataService._cases,
        DemoDataService._initialized,
    )
    DemoDataService._instance = None
    DemoDataService._initialized = False
    yield
    (
        DemoDataService._instance,
        DemoDataService._findings,
        DemoDataService._cases,
        DemoDataService._initialized,
    ) = saved


def _connected() -> tuple[DatabaseDataService, MagicMock]:
    service = DatabaseDataService()
    db = MagicMock()
    db.get_cases.return_value = []
    service._db_service = db
    service._db_connected = True
    return service, db


def test_a_service_built_before_the_switch_follows_it(
    state_dir, database, fresh_estate
):
    service, db = _connected()
    assert service.get_backend_info()["backend"] == "postgresql"

    reply = set_demo_mode(DemoModeConfig(enabled=True), USER)
    assert database[DEMO_MODE_CONFIG_KEY] == {"enabled": True}
    assert "restart" not in reply["message"].lower()
    assert service.get_backend_info()["backend"] == "demo"
    assert service.is_using_database() is False
    assert [c["case_id"] for c in service.get_cases()][:1] == ["case-demo-01"]
    assert service.get_finding("f-demo-a5")["severity"] == "critical"
    db.get_cases.assert_not_called()

    reply = set_demo_mode(DemoModeConfig(enabled=False), USER)
    assert "restart" not in reply["message"].lower()
    assert service.get_backend_info()["backend"] == "postgresql"
    assert service.get_cases() == []
    db.get_cases.assert_called_once()


def test_another_process_sees_the_switch_within_seconds(
    state_dir, database, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("core.config.time.monotonic", lambda: clock[0])
    assert is_demo_mode() is False

    database[DEMO_MODE_CONFIG_KEY] = {"enabled": True}  # another replica's write
    assert is_demo_mode() is False  # this process still trusts its last read
    clock[0] += 5
    assert is_demo_mode() is True


def _database_down(monkeypatch):
    def down(user_id: str = "system"):
        raise ConnectionError("postgres is down")

    monkeypatch.setattr("core.storage.config_service.get_config_service", down)
    monkeypatch.setattr(config_router, "get_config_service", down)


def test_the_row_beats_the_file(state_dir, database):
    (state_dir / "general_config.json").write_text(json.dumps({"demo_mode": True}))
    database[DEMO_MODE_CONFIG_KEY] = {"enabled": False}
    assert is_demo_mode() is False


def test_an_outage_keeps_the_last_answer_not_this_pods_file(
    state_dir, database, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("core.config.time.monotonic", lambda: clock[0])
    database[DEMO_MODE_CONFIG_KEY] = {"enabled": False}
    assert is_demo_mode() is False

    (state_dir / "general_config.json").write_text(json.dumps({"demo_mode": True}))
    _database_down(monkeypatch)
    clock[0] += 5
    assert is_demo_mode() is False


def test_the_file_answers_until_the_database_first_has(state_dir, monkeypatch):
    (state_dir / "general_config.json").write_text(json.dumps({"demo_mode": True}))
    _database_down(monkeypatch)
    assert is_demo_mode() is True


def test_a_switch_the_database_cannot_save_changes_nothing(
    state_dir, database, monkeypatch
):
    (state_dir / "general_config.json").write_text(json.dumps({"demo_mode": False}))

    class _Unwritable:
        def __init__(self, user_id: str = "system"):
            pass

        def set_system_config(self, key, value, **_):
            return False

    monkeypatch.setattr(config_router, "get_config_service", _Unwritable)

    with pytest.raises(HTTPException) as refused:
        set_demo_mode(DemoModeConfig(enabled=True), USER)

    assert refused.value.status_code == 503
    saved = json.loads((state_dir / "general_config.json").read_text())
    assert saved == {"demo_mode": False}
    assert is_demo_mode() is False


def test_the_environment_still_wins_and_the_reply_says_so(
    state_dir, database, monkeypatch
):
    from core.config import get_settings

    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()

    reply = set_demo_mode(DemoModeConfig(enabled=True), USER)

    assert "environment" in reply["message"]
    assert database[DEMO_MODE_CONFIG_KEY] == {"enabled": True}
    assert DatabaseDataService().is_demo_mode() is False


def test_saving_general_settings_keeps_demo_mode_in_the_file(
    state_dir, database, monkeypatch
):
    monkeypatch.setattr(config_router, "get_secrets_manager", lambda **_: None)
    monkeypatch.setattr("core.secrets_manager._secrets_manager", None)
    set_demo_mode(DemoModeConfig(enabled=True), USER)

    config_router.set_general_config(GeneralConfig(theme="light"), USER)

    saved = json.loads((state_dir / "general_config.json").read_text())
    assert saved["demo_mode"] is True
    assert saved["theme"] == "light"
    assert not list(state_dir.glob(".*.tmp"))


def test_the_console_routes_follow_the_switch(
    state_dir, database, fresh_estate, monkeypatch
):
    db = MagicMock()
    db.count_findings.return_value = 0
    db.get_findings.return_value = []
    monkeypatch.setattr(v1_findings.data_service, "_db_service", db)
    monkeypatch.setattr(v1_findings.data_service, "_db_connected", True)
    monkeypatch.setattr(
        "core.auth.permissions.AuthService.check_permission", lambda *a, **k: True
    )
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        client = TestClient(app)
        assert client.get("/api/v1/findings").json()["total"] == 0

        on = client.post("/api/config/demo-mode", json={"enabled": True})
        assert on.status_code == 200, on.text
        body = client.get("/api/v1/findings").json()
        assert body["total"] == 23
        assert body["findings"][0]["finding_id"] == "f-demo-n6"

        off = client.post("/api/config/demo-mode", json={"enabled": False})
        assert off.status_code == 200, off.text
        assert client.get("/api/v1/findings").json()["total"] == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)
