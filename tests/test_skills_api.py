"""API-level tests for the Skill Builder (Issue #82).

The Skills router is mounted on a throwaway FastAPI app with SkillService
patched to an in-memory store — this keeps the tests fast and unaffected
by whether Postgres is running.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# In-memory fake so the router under test doesn't need a live DB / Claude.
# ---------------------------------------------------------------------------


class FakeSkillService:
    _next_id = 1

    def __init__(self):
        pass

    # NB the module-level store is populated in the fixture below.
    _store: dict = {}
    _gen_response: dict = {}

    def list_skills(self, category=None, is_active=None):
        items = list(self._store.values())
        if category:
            items = [s for s in items if s["category"] == category]
        if is_active is not None:
            items = [s for s in items if s["is_active"] == is_active]
        return items

    def get_skill(self, skill_id):
        return self._store.get(skill_id)

    def create_skill(self, data, created_by=None):
        FakeSkillService._next_id += 1
        skill_id = f"s-20260421-{FakeSkillService._next_id:08X}"
        skill = {
            "skill_id": skill_id,
            "version": 1,
            "created_by": created_by or data.get("created_by"),
            "created_at": "2026-04-21T00:00:00",
            "updated_at": "2026-04-21T00:00:00",
            **data,
        }
        # Defaults must match response schema
        skill.setdefault("description", None)
        skill.setdefault("input_schema", {})
        skill.setdefault("output_schema", {})
        skill.setdefault("required_tools", [])
        skill.setdefault("execution_steps", [])
        skill.setdefault("is_active", True)
        self._store[skill_id] = skill
        return skill

    def update_skill(self, skill_id, patch):
        row = self._store.get(skill_id)
        if not row:
            return None
        content_fields = {
            "name",
            "description",
            "category",
            "input_schema",
            "output_schema",
            "required_tools",
            "prompt_template",
            "execution_steps",
        }
        bumped = False
        for key, value in patch.items():
            if value is None:
                continue
            if key in content_fields and row.get(key) != value:
                bumped = True
            row[key] = value
        if bumped:
            row["version"] = row.get("version", 1) + 1
        return row

    def delete_skill(self, skill_id):
        return self._store.pop(skill_id, None) is not None

    async def generate_skill(
        self, description, category=None, conversation_history=None
    ):
        return self._gen_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    # Reset the fake between tests
    FakeSkillService._store = {}
    FakeSkillService._gen_response = {}

    with patch("services.skill_service.SkillService", FakeSkillService):
        # Import late so the patched class is picked up by backend.api.skills
        spec = importlib.util.spec_from_file_location(
            "skills_router_under_test",
            _BACKEND_DIR / "api" / "skills.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["skills_router_under_test"] = mod
        spec.loader.exec_module(mod)

        app = FastAPI()
        app.include_router(mod.router, prefix="/api/skills")
        yield TestClient(app)


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


VALID_SKILL_PAYLOAD = {
    "name": "Detect Lateral RDP",
    "description": "Detects suspicious RDP in last 24h.",
    "category": "detection",
    "input_schema": {"type": "object", "properties": {"hours": {"type": "integer"}}},
    "output_schema": {"type": "object"},
    "required_tools": ["splunk.search"],
    "prompt_template": "Look for RDP in last {{hours}} hours.",
    "execution_steps": [
        {"step_id": "1", "type": "mcp_tool_call", "tool": "splunk.search"}
    ],
    "is_active": True,
}


@pytest.mark.api
def test_create_list_get_delete(client):
    # CREATE
    r = client.post("/api/skills", json=VALID_SKILL_PAYLOAD)
    assert r.status_code == 201, r.text
    created = r.json()
    skill_id = created["skill_id"]
    assert created["name"] == VALID_SKILL_PAYLOAD["name"]
    assert created["version"] == 1

    # LIST
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # FILTER BY CATEGORY
    r = client.get("/api/skills", params={"category": "detection"})
    assert len(r.json()) == 1
    r = client.get("/api/skills", params={"category": "enrichment"})
    assert len(r.json()) == 0

    # GET BY ID
    r = client.get(f"/api/skills/{skill_id}")
    assert r.status_code == 200
    assert r.json()["skill_id"] == skill_id

    # DELETE
    r = client.delete(f"/api/skills/{skill_id}")
    assert r.status_code == 200
    assert r.json() == {"success": True, "skill_id": skill_id}

    # 404 after delete
    r = client.get(f"/api/skills/{skill_id}")
    assert r.status_code == 404


@pytest.mark.api
def test_update_bumps_version_on_content_change(client):
    r = client.post("/api/skills", json=VALID_SKILL_PAYLOAD)
    skill_id = r.json()["skill_id"]

    # is_active flip alone must NOT bump version
    r = client.put(f"/api/skills/{skill_id}", json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert r.json()["is_active"] is False

    # Content change bumps version
    r = client.put(f"/api/skills/{skill_id}", json={"description": "Updated"})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["description"] == "Updated"


@pytest.mark.api
def test_get_missing_returns_404(client):
    r = client.get("/api/skills/s-00000000-DEADBEEF")
    assert r.status_code == 404


@pytest.mark.api
def test_delete_missing_returns_404(client):
    r = client.delete("/api/skills/s-00000000-DEADBEEF")
    assert r.status_code == 404


@pytest.mark.api
def test_create_rejects_invalid_category(client):
    bad = dict(VALID_SKILL_PAYLOAD)
    bad["category"] = "nonsense"
    r = client.post("/api/skills", json=bad)
    assert r.status_code == 422  # Pydantic validation


# ---------------------------------------------------------------------------
# Generate endpoint — multi-turn clarification
# ---------------------------------------------------------------------------


@pytest.mark.api
def test_generate_passes_through_clarification(client):
    FakeSkillService._gen_response = {
        "success": True,
        "needs_clarification": True,
        "message": "I have some questions: which SIEM?",
        "conversation_history": [
            {"role": "user", "content": "detect RDP"},
            {"role": "assistant", "content": "I have some questions: which SIEM?"},
        ],
    }
    r = client.post(
        "/api/skills/generate",
        json={"description": "detect lateral RDP", "category": "detection"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["needs_clarification"] is True
    assert "which SIEM" in body["message"]
    assert body["conversation_history"][-1]["role"] == "assistant"


@pytest.mark.api
def test_generate_returns_skill_draft(client):
    FakeSkillService._gen_response = {
        "success": True,
        "needs_clarification": False,
        "skill": {
            "name": "Detect Lateral RDP",
            "description": "x",
            "category": "detection",
            "input_schema": {},
            "output_schema": {},
            "required_tools": ["splunk.search"],
            "prompt_template": "do it",
            "execution_steps": [],
            "is_active": True,
        },
        "message": "Generated skill 'Detect Lateral RDP'",
    }
    r = client.post("/api/skills/generate", json={"description": "detect RDP"})
    assert r.status_code == 200
    body = r.json()
    assert body["needs_clarification"] is False
    assert body["skill"]["name"] == "Detect Lateral RDP"
