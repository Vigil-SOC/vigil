"""Tests for the per-request unit-of-work boundary."""

from core.routing import UnitOfWorkSession
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import core.storage.unit_of_work as uow_module

pytestmark = pytest.mark.unit


@pytest.fixture
def session(monkeypatch):
    """Patch the session factory so the boundary operates on a mock."""
    mock_session = MagicMock()
    monkeypatch.setattr(uow_module, "get_db_session", lambda: mock_session)
    return mock_session


@pytest.fixture
def client(session):
    """An app whose endpoints exercise the boundary in one request each."""
    app = FastAPI()

    @app.post("/two-writes")
    def two_writes(db: UnitOfWorkSession):
        db.add("first")
        db.add("second")
        return {"ok": True}

    @app.post("/fails-midway")
    def fails_midway(db: UnitOfWorkSession):
        db.add("first")
        raise RuntimeError("second step blew up")

    @app.post("/rejects")
    def rejects(db: UnitOfWorkSession):
        db.add("first")
        raise HTTPException(status_code=409, detail="conflict")

    return TestClient(app, raise_server_exceptions=False)


def test_commits_once_for_the_whole_request(client, session):
    assert client.post("/two-writes").status_code == 200

    assert session.add.call_count == 2
    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    session.close.assert_called_once()


def test_multi_step_write_rolls_back_fully_on_mid_operation_failure(client, session):
    assert client.post("/fails-midway").status_code == 500

    # The first step ran, but nothing reached the database.
    session.add.assert_called_once_with("first")
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_rejecting_with_http_exception_rolls_back(client, session):
    response = client.post("/rejects")

    assert response.status_code == 409
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_failed_commit_is_reported_as_an_error(session):
    """A commit that fails must not answer with the endpoint's success body.

    This is what ``scope="function"`` on the dependency buys: under the default
    request scope the response is already sent by the time the commit runs.
    """
    session.commit.side_effect = RuntimeError("deadlock detected")
    app = FastAPI()

    @app.post("/writes")
    def writes(db: UnitOfWorkSession):
        db.add("row")
        return {"ok": True}

    response = TestClient(app, raise_server_exceptions=False).post("/writes")

    assert response.status_code == 500
    assert "ok" not in response.text
    session.rollback.assert_called_once()
    session.close.assert_called_once()
