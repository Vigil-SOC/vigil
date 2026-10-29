"""A gate checks the name it declares, and an unreadable role denies."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import current_user as current_user_module
from core.auth.permissions import missing_permission, permission_gate
from core.storage.models import Principal, Role, User
from core.storage.models.base import Base

pytestmark = pytest.mark.unit


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[Principal.__table__, Role.__table__, User.__table__])
    with sessionmaker(bind=engine)() as s:
        # Holds the pre-catalog parent, not the split name the gate declares.
        s.add(
            Role(
                role_id="r",
                name="r",
                description="",
                permissions={"settings.write": True, "settings.read": True},
            )
        )
        s.add(Principal(principal_id="u", kind="person", label="u"))
        s.add(
            User(
                user_id="u",
                principal_id="u",
                username="u",
                email="u@example.com",
                password_hash="x",
                full_name="u",
                role_id="r",
                is_active=True,
            )
        )
        s.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        s.commit()
        yield s


@pytest.mark.parametrize("version", [None, "0007_tool_effect_classes", "0008_permission_catalog"])
def test_a_role_lacking_the_declared_name_is_refused_whatever_the_revision(
    session, version
):
    session.execute(text("DELETE FROM alembic_version"))
    if version:
        session.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": version})
    session.commit()

    for name in ("detections.write", "automation.write", "platform.write", "ai_autonomy.write"):
        assert missing_permission("u", name, session) == name


def _unreachable(monkeypatch):
    class _Closed:
        def connection(self, *_a, **_k):
            raise OperationalError("SELECT 1", {}, Exception("down"))

        query = connection

    @contextmanager
    def _down(_session=None):
        yield _Closed()

    monkeypatch.setattr("core.auth.permissions.unit_of_work", _down)
    monkeypatch.setattr("core.auth.auth_service.unit_of_work", _down)
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)


def test_an_unreachable_database_denies(monkeypatch):
    _unreachable(monkeypatch)

    assert missing_permission("analyst", "cases.read") == "cases.read"


def test_a_gated_route_is_refused_with_the_database_unreachable(monkeypatch):
    _unreachable(monkeypatch)
    app = FastAPI()

    @app.post("/gated", dependencies=[permission_gate("platform.write")])
    def _gated():
        return {"ok": True}

    app.dependency_overrides[current_user_module.get_current_user] = (
        lambda: SimpleNamespace(user_id="admin")
    )

    response = TestClient(app).post("/gated")

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: platform.write required"


def test_a_stubbed_check_still_answers_without_a_database(monkeypatch):
    _unreachable(monkeypatch)
    monkeypatch.setattr(
        "core.auth.permissions.AuthService.check_permission",
        staticmethod(lambda user_id, permission, session=None: permission == "cases.read"),
    )

    assert missing_permission("analyst", "cases.read") is None
    assert missing_permission("analyst", "cases.write") == "cases.write"


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
