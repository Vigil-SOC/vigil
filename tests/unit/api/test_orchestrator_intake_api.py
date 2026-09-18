"""GET /api/orchestrator/intake lists trigger rows (#919).

DB access is mocked so the route test does not need Postgres. The daemon
crossing behaviour lives in ``test_orchestrator_intake.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers.orchestrator import router as orchestrator_router

pytestmark = pytest.mark.unit

OLDER = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
NEWER = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _row(*, id: int, state: str, created_at: datetime, finding_id: str = "f-1"):
    return SimpleNamespace(
        id=id,
        kind="detection",
        state=state,
        reason=None,
        finding_id=finding_id,
        priority="high",
        payload={},
        investigation_id=None,
        merged_into=None,
        created_at=created_at,
        decided_at=None,
    )


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter_by(self, **kwargs):
        if "state" in kwargs:
            self.rows = [r for r in self.rows if r.state == kwargs["state"]]
        return self

    def order_by(self, *args):
        self.rows = sorted(self.rows, key=lambda r: r.created_at, reverse=True)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def all(self):
        return list(self.rows)

    def count(self):
        return len(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        return _Query(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def client(monkeypatch):
    rows = [
        _row(id=1, state="queued", created_at=OLDER, finding_id="f-old"),
        _row(id=2, state="queued", created_at=NEWER, finding_id="f-new"),
        _row(id=3, state="launched", created_at=NEWER, finding_id="f-done"),
    ]
    db = MagicMock()
    db.session_scope.return_value = _Session(rows)
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    app = FastAPI()
    app.include_router(orchestrator_router, prefix="/api/orchestrator")
    return TestClient(app)


def test_intake_list_returns_newest_first_in_the_investigations_envelope(client):
    resp = client.get("/api/orchestrator/intake")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"triggers", "count"}
    assert body["count"] == 3
    assert [row["id"] for row in body["triggers"]] == [2, 3, 1]


def test_intake_list_filters_by_state(client):
    resp = client.get("/api/orchestrator/intake", params={"state": "queued"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert {row["state"] for row in body["triggers"]} == {"queued"}


def test_intake_list_honours_limit(client):
    resp = client.get("/api/orchestrator/intake", params={"limit": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["triggers"][0]["id"] == 2


def test_intake_list_does_not_construct_an_orchestrator(client, monkeypatch):
    monkeypatch.setattr(
        "services.api.routers.orchestrator._get_orchestrator",
        lambda: (_ for _ in ()).throw(AssertionError("must not build an orchestrator")),
    )

    resp = client.get("/api/orchestrator/intake")

    assert resp.status_code == 200
    assert resp.json()["count"] == 3


def _patch_status_orchestrator(monkeypatch):
    orch = MagicMock()
    orch.get_all_investigations.return_value = [
        {"status": "assigned"},
        {"status": "queued"},
        {"status": "completed"},
    ]
    orch.get_cost_summary.return_value = {}
    orch.stats = {}
    orch.enabled = False
    monkeypatch.setattr(
        "services.api.routers.orchestrator._get_orchestrator", lambda: orch
    )
    cfg = MagicMock()
    cfg.get_system_config.return_value = {
        "enabled": False,
        "max_concurrent_agents": 3,
    }
    monkeypatch.setattr("core.storage.config_service.get_config_service", lambda: cfg)


def test_status_queued_is_intake_depth_not_investigation_status(client, monkeypatch):
    _patch_status_orchestrator(monkeypatch)

    resp = client.get("/api/orchestrator/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 2
    assert body["completed"] == 1
    assert body["active_agents"] == 1


def test_status_queued_does_not_report_zero_when_intake_count_fails(monkeypatch):
    _patch_status_orchestrator(monkeypatch)
    db = MagicMock()
    db.session_scope.side_effect = RuntimeError("intake table unreachable")
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    app = FastAPI()
    app.include_router(orchestrator_router, prefix="/api/orchestrator")
    resp = TestClient(app).get("/api/orchestrator/status")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "intake table unreachable"}
