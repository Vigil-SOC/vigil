"""A database failure on the AI-decision routes answers 500, not empty data."""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from core.storage import service as service_module
from services.api import main as backend_main

pytestmark = pytest.mark.unit


class DownDB:
    @contextmanager
    def session_scope(self):
        raise OperationalError("SELECT 1", {}, Exception("db down"))
        yield


class EmptyQuery:
    def filter(self, *_):
        return self

    def first(self):
        return None


class EmptyDB:
    @contextmanager
    def session_scope(self):
        class Session:
            def query(self, *_):
                return EmptyQuery()

        yield Session()


def _client(monkeypatch, authenticate_app, db):
    monkeypatch.setattr(service_module, "get_db_manager", lambda: db)
    authenticate_app(backend_main.app)
    return TestClient(backend_main.app, raise_server_exceptions=False)


@pytest.fixture()
def down(monkeypatch, authenticate_app):
    return _client(monkeypatch, authenticate_app, DownDB())


@pytest.mark.parametrize(
    "path",
    [
        "/api/ai/decisions",
        "/api/ai/decisions/pending-feedback",
        "/api/ai/decisions/stats",
    ],
)
def test_reads_fail_loud(down, path):
    resp = down.get(path)
    assert resp.status_code == 500, (resp.status_code, resp.text)


def test_feedback_fails_loud(down):
    resp = down.post(
        "/api/ai/decisions/dec-1/feedback", json={"human_decision": "agree"}
    )
    assert resp.status_code == 500, (resp.status_code, resp.text)


def test_feedback_on_an_unknown_decision_is_a_404(monkeypatch, authenticate_app):
    client = _client(monkeypatch, authenticate_app, EmptyDB())
    resp = client.post(
        "/api/ai/decisions/nope/feedback", json={"human_decision": "agree"}
    )
    assert resp.status_code == 404, resp.text
