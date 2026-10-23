"""The catalog revision decides which permission name a gate checks."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth.permissions import enforced_permission, missing_permission

pytestmark = pytest.mark.unit


def test_without_the_revision_a_split_write_keeps_its_parent(monkeypatch):
    monkeypatch.setattr(
        "core.auth.permissions.catalog_revision_applied", lambda session: False
    )
    session = object()

    assert enforced_permission("detections.write", session) == "settings.write"
    assert enforced_permission("automation.write", session) == "settings.write"
    assert enforced_permission("platform.write", session) == "settings.write"
    assert enforced_permission("integrations.install", session) == "integrations.write"
    # New gates stay open. Live gates stay enforced.
    assert enforced_permission("detections.read", session) is None
    assert enforced_permission("cases.assign", session) is None
    assert enforced_permission("ai_decisions.read", session) is None
    assert enforced_permission("cases.write", session) == "cases.write"
    assert enforced_permission("loglm.view", session) == "loglm.view"


def test_with_the_revision_the_declared_name_is_the_check(monkeypatch):
    monkeypatch.setattr(
        "core.auth.permissions.catalog_revision_applied", lambda session: True
    )

    assert enforced_permission("detections.write", object()) == "detections.write"
    assert enforced_permission("detections.read", object()) == "detections.read"


def test_overview_names_a_missing_read_and_keeps_the_arrays(monkeypatch):
    from core.auth.current_user import get_current_user
    from services.api.routers import overview

    monkeypatch.setattr(
        overview,
        "overview_payload",
        lambda: {
            "arrivals": [{"count": 1}],
            "outcomes": [{"state": "ticket"}],
            "feed": [{"finding_id": "f-1"}],
            "agents": [{"workflow_id": "respond"}],
        },
    )

    def _missing(user_id, permission, session=None):
        return permission if permission == "findings.read" else None

    monkeypatch.setattr(overview, "missing_permission", _missing)
    app = FastAPI()
    app.include_router(overview.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="viewer"
    )

    body = TestClient(app).get("/api/overview").json()

    assert body["arrivals"] == []
    assert body["outcomes"] == []
    assert body["feed"] == []
    assert body["agents"] == [{"workflow_id": "respond"}]
    assert body["missing"] == ["findings.read"]
    assert body["feed"] == []  # still an array: the console calls feed.find


def test_an_alert_read_names_findings_read(monkeypatch):
    from core.auth.current_user import get_current_user
    from services.api.routers import overview

    monkeypatch.setattr(overview, "overview_alert", lambda finding_id: {"finding_id": finding_id})

    def _deny(user_id, permission, session=None):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403, detail=f"Permission denied: {permission} required"
        )

    monkeypatch.setattr("core.auth.permissions.deny_unless", _deny)
    app = FastAPI()
    app.include_router(overview.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="viewer")

    response = TestClient(app).get("/api/overview/alerts/f-1")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: findings.read required"


def test_missing_permission_reports_the_parent_before_the_revision(monkeypatch):
    monkeypatch.setattr(
        "core.auth.permissions.catalog_revision_applied", lambda session: False
    )
    monkeypatch.setattr(
        "core.auth.permissions.AuthService.check_permission",
        staticmethod(lambda user_id, permission, session=None: False),
    )

    assert missing_permission("analyst", "detections.write", object()) == "settings.write"
