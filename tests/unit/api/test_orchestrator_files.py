from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import orchestrator as router
from services.daemon.workdir import WorkdirManager

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path):
    (tmp_path / "secret.env").write_text("DB_PASSWORD=hunter2")
    wm = WorkdirManager(str(tmp_path / "investigations"))
    wm.create("inv-1")
    wm.create("inv-10")
    wm.write_file("inv-1", "context.md", "INV-1 BRIEF")
    wm.write_file("inv-10", "context.md", "INV-10 BRIEF")
    app = FastAPI()
    app.include_router(router.router, prefix="/api/orchestrator")
    with patch.object(
        router, "_get_orchestrator", return_value=SimpleNamespace(workdir=wm)
    ):
        yield TestClient(app)


def test_serves_the_investigations_own_file(client):
    res = client.get("/api/orchestrator/investigations/inv-1/files/context.md")
    assert res.status_code == 200
    assert res.json()["content"] == "INV-1 BRIEF"


@pytest.mark.parametrize(
    "url",
    [
        "/api/orchestrator/investigations/%2E%2E/files/secret.env",
        "/api/orchestrator/investigations/inv-1/files/..%2Finv-10%2Fcontext.md",
    ],
)
def test_serves_nothing_outside_the_investigation(client, url):
    res = client.get(url)
    assert "hunter2" not in res.text
    assert "INV-10" not in res.text
