from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from core.storage.database_data_service import DatabaseDataService
from core.storage.service import DatabaseService

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
os.environ.setdefault("DEV_MODE", "true")

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _demo_off():
    with patch("core.storage.database_data_service.is_demo_mode", return_value=False):
        yield


class _DeadManager:
    @contextmanager
    def session_scope(self):
        raise OperationalError(
            "SELECT 1", {}, Exception("server closed the connection")
        )
        yield


def _failing_data_service() -> DatabaseDataService:
    svc = DatabaseDataService()
    db = DatabaseService.__new__(DatabaseService)
    db.db_manager = _DeadManager()
    svc._db_service = db
    svc._db_connected = True
    return svc


@pytest.mark.parametrize(
    "read",
    [
        lambda s: s.get_findings(),
        lambda s: s.count_findings(),
        lambda s: s.get_finding("f-1"),
    ],
)
def test_read_raises_when_the_query_fails(read):
    with pytest.raises(OperationalError):
        read(_failing_data_service())


def test_no_database_is_still_an_empty_read():
    svc = DatabaseDataService()
    with patch(
        "core.storage.database_data_service.init_database",
        side_effect=RuntimeError("postgres down"),
    ):
        assert svc.get_findings() == []
        assert svc.count_findings() == 0
        assert svc.get_finding("f-1") is None


@pytest.mark.parametrize(
    "path",
    ["/api/v1/findings/", "/api/findings/", "/api/v1/findings/f-1"],
)
def test_route_answers_500_when_the_query_fails(path, authenticate_app):
    import core.api.v1.findings_router as findings_router
    from services.api.main import app

    authenticate_app(app)
    with patch.object(findings_router, "data_service", _failing_data_service()):
        response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == 500, response.text
