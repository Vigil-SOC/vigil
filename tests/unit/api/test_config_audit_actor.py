"""Authenticated config writes are audited as the signed-in user."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_integration_bridge
from core.storage.models import ConfigAuditLog, IntegrationConfig, SystemConfig
from services.api.middleware.auth import get_current_active_user

pytestmark = pytest.mark.unit


class _Query:
    def __init__(self, rows, model):
        self._rows = rows
        self._model = model
        self._filters: dict = {}

    def filter_by(self, **kwargs):
        self._filters = kwargs
        return self

    def first(self):
        for row in self._rows:
            if type(row) is not self._model:
                continue
            if all(getattr(row, key) == value for key, value in self._filters.items()):
                return row
        return None


class _Session:
    def __init__(self):
        self.rows: list = []

    def query(self, model):
        return _Query(self.rows, model)

    def add(self, obj):
        if obj not in self.rows:
            self.rows.append(obj)

    def commit(self):
        return None


def _client(authenticate_app, session):
    from core.ingestion.kafka_router import router as kafka_router
    from services.api.routers.budgets import router as budgets_router
    from services.api.routers.config import router as config_router
    from services.api.routers.federation import router as federation_router
    from services.api.routers.mcp import router as mcp_router
    from services.api.routers.orchestrator import router as orchestrator_router

    app = FastAPI()
    auth = [Depends(get_current_active_user)]
    app.include_router(config_router, prefix="/api/config", dependencies=auth)
    app.include_router(federation_router, prefix="/api/federation", dependencies=auth)
    app.include_router(kafka_router, prefix="/api/kafka", dependencies=auth)
    app.include_router(mcp_router, prefix="/api/mcp", dependencies=auth)
    app.include_router(budgets_router, prefix="/api", dependencies=auth)
    app.include_router(
        orchestrator_router, prefix="/api/orchestrator", dependencies=auth
    )
    user = authenticate_app(app)
    app.dependency_overrides[provide_integration_bridge] = lambda: MagicMock()

    @contextmanager
    def fake_session():
        yield session

    return TestClient(app), user, fake_session


def _changed_by(session, config_key):
    matches = [
        row
        for row in session.rows
        if isinstance(row, ConfigAuditLog) and row.config_key == config_key
    ]
    assert matches, config_key
    return matches[-1].changed_by


def test_signed_in_user_is_who_changed_the_config(
    authenticate_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    monkeypatch.setattr(
        "services.api.routers.orchestrator._get_orchestrator", lambda: None
    )
    session = _Session()
    client, user, fake_session = _client(authenticate_app, session)
    actor = str(user.user_id)
    monkeypatch.setattr("core.storage.config_service.get_session", fake_session)

    saved = client.post(
        "/api/config/integrations",
        json={
            "enabled_integrations": ["lab"],
            "integrations": {"lab": {"host": "lab.example"}},
        },
    )
    assert saved.status_code == 200, saved.text
    integration = (
        _Query(session.rows, IntegrationConfig).filter_by(integration_id="lab").first()
    )
    assert integration is not None
    assert integration.updated_by == actor
    assert _changed_by(session, "lab") == actor

    federation = client.put("/api/federation/settings", json={"enabled": True})
    assert federation.status_code == 200, federation.text
    fed_row = (
        _Query(session.rows, SystemConfig).filter_by(key="federation.settings").first()
    )
    assert fed_row is not None
    assert fed_row.updated_by == actor
    assert _changed_by(session, "federation.settings") == actor

    kafka = client.put("/api/kafka/config", json={})
    assert kafka.status_code == 200, kafka.text
    kafka_row = (
        _Query(session.rows, SystemConfig).filter_by(key="kafka.settings").first()
    )
    assert kafka_row is not None
    assert kafka_row.updated_by == actor
    assert _changed_by(session, "kafka.settings") == actor

    surface = client.put("/api/mcp/surface", json={"enabled": True})
    assert surface.status_code == 200, surface.text
    mcp_row = (
        _Query(session.rows, SystemConfig).filter_by(key="mcp.server_enabled").first()
    )
    assert mcp_row is not None
    assert mcp_row.updated_by == actor
    assert _changed_by(session, "mcp.server_enabled") == actor

    budget = client.put(
        "/api/analytics/budget",
        json={
            "default_vk": "sk-bf-test",
            "budget_limit_usd": 1,
            "enforcement_mode": "warning",
        },
    )
    assert budget.status_code == 200, budget.text
    budget_row = (
        _Query(session.rows, SystemConfig).filter_by(key="bifrost.virtual_keys").first()
    )
    assert budget_row is not None
    assert budget_row.updated_by == actor
    assert _changed_by(session, "bifrost.virtual_keys") == actor

    enabled = client.post("/api/orchestrator/enable")
    assert enabled.status_code == 200, enabled.text
    orch_row = (
        _Query(session.rows, SystemConfig)
        .filter_by(key="orchestrator.settings")
        .first()
    )
    assert orch_row is not None
    assert orch_row.updated_by == actor
    assert orch_row.value["enabled"] is True
    assert _changed_by(session, "orchestrator.settings") == actor
