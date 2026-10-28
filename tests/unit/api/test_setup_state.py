"""The setup wizard's place, kept for the install (GET/PUT /api/config/setup-state)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def client(throwaway_database):
    from core.storage.models import SystemConfig
    from core.storage.unit_of_work import unit_of_work
    from services.api.middleware.auth import get_current_active_user
    from services.api.routers import config

    def purge():
        with unit_of_work() as session:
            session.query(SystemConfig).filter(
                SystemConfig.key == config.SETUP_STATE_KEY
            ).delete()

    app = FastAPI()
    app.include_router(config.router, prefix=config.ROUTER_META.prefix)
    for gate in (config._SETTINGS_READ, config._SETTINGS_WRITE):
        app.dependency_overrides[gate[0].dependency] = lambda: None
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="u-admin", username="admin"
    )
    purge()
    yield TestClient(app)
    purge()


def test_a_new_install_has_not_started(client):
    assert client.get("/api/config/setup-state").json() == {
        "furthest": 1,
        "passed": [],
        "finished": False,
        "finished_by": None,
        "finished_at": None,
    }


def test_progress_only_grows_so_two_admins_cannot_undo_each_other(client):
    client.put("/api/config/setup-state", json={"furthest": 3, "passed": [1, 2]})
    body = client.put(
        "/api/config/setup-state", json={"furthest": 2, "passed": [1, 4]}
    ).json()
    assert body["furthest"] == 3 and body["passed"] == [1, 2, 4]
    assert client.get("/api/config/setup-state").json()["passed"] == [1, 2, 4]


def test_finishing_records_who_and_when_once(client):
    first = client.put("/api/config/setup-state", json={"finished": True}).json()
    assert first["finished"] and first["finished_by"] == "admin"
    again = client.put("/api/config/setup-state", json={"finished": True}).json()
    assert again["finished_at"] == first["finished_at"]
    # a finish is not taken back
    assert client.put("/api/config/setup-state", json={"finished": False}).json()[
        "finished"
    ]


def test_steps_outside_the_wizard_are_refused(client):
    assert (
        client.put("/api/config/setup-state", json={"passed": [0, 6]}).status_code
        == 422
    )
    assert (
        client.put("/api/config/setup-state", json={"furthest": 9}).status_code == 422
    )
