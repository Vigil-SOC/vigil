"""``GET /api/skills`` lists the skills the loader finds on disk (#928).

The router is mounted on a throwaway FastAPI app with ``skill_roots`` pointed
at the fixture directory, so the test needs neither Postgres nor a setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import skills as skills_router

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(skills_router, "skill_roots", lambda: [FIXTURES])
    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/skills")
    return TestClient(app)


def test_list_returns_loaded_skills_with_source_path(client):
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()}
    assert set(by_name) == {"full-skill", "minimal-skill"}
    assert by_name["minimal-skill"]["source_path"] == str(FIXTURES / "minimal-skill")
    assert by_name["minimal-skill"]["description"].startswith("The smallest skill")
    assert set(by_name["minimal-skill"]) == {"name", "description", "source_path"}


def test_write_endpoints_are_gone(client):
    assert client.post("/api/skills", json={"name": "x"}).status_code == 405
    assert client.get("/api/skills/some-id").status_code == 404
