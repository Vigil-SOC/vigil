"""Switching demo mode applies at once to services already built: no restart.

Most callers hold a ``DatabaseDataService`` built at import, so the switch has to
reach them through the saved setting, not through a new instance.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core.platform.demo_data_service import DemoDataService
from core.storage.database_data_service import DatabaseDataService
from services.api.routers.config import DemoModeConfig, set_demo_mode

pytestmark = pytest.mark.unit


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


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


def test_a_service_built_before_the_switch_follows_it(state_dir, fresh_estate):
    service, db = _connected()
    assert service.get_backend_info()["backend"] == "postgresql"

    reply = set_demo_mode(DemoModeConfig(enabled=True))
    assert "restart" not in reply["message"].lower()
    assert service.get_backend_info()["backend"] == "demo"
    assert service.is_using_database() is False
    assert [c["case_id"] for c in service.get_cases()][:1] == ["case-demo-01"]
    assert service.get_finding("f-demo-a5")["severity"] == "critical"
    db.get_cases.assert_not_called()

    reply = set_demo_mode(DemoModeConfig(enabled=False))
    assert "restart" not in reply["message"].lower()
    assert service.get_backend_info()["backend"] == "postgresql"
    assert service.get_cases() == []
    db.get_cases.assert_called_once()


def test_the_environment_still_wins_and_the_reply_says_so(state_dir, monkeypatch):
    from core.config import get_settings

    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()

    reply = set_demo_mode(DemoModeConfig(enabled=True))

    assert "environment" in reply["message"]
    saved = json.loads((state_dir / "general_config.json").read_text())
    assert saved["demo_mode"] is True
    assert DatabaseDataService().is_demo_mode() is False
