"""``/api/skills`` lists skills on disk and writes under the operator root.

The list fixture mounts the router with ``skill_roots`` pointed at the fixture
directory. Write tests use a temp operator root and the real loader, so a
prompt built afterwards reads the file that was just written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents.prompts import render_base_prompt
from core.config import Settings
from core.skills.skill_library import LIBRARY_ROOT, parse_skill
from services.api.routers import skills as skills_router

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BUNDLED = "phishing-triage"
BUNDLED_FILE = LIBRARY_ROOT / BUNDLED / "SKILL.md"


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/skills")
    return TestClient(app)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(skills_router, "skill_roots", lambda: [FIXTURES])
    return _app()


@pytest.fixture()
def operator(tmp_path, monkeypatch):
    root = tmp_path / "operator"
    root.mkdir()
    settings = Settings(vigil_skills_path=str(root))
    monkeypatch.setattr("core.skills.skill_library.get_settings", lambda: settings)
    return _app(), root


def _write(client: TestClient, name: str, description: str = "A saved skill.", body: str = "# Saved\n"):
    return client.post(
        "/api/skills",
        json={"name": name, "description": description, "body": body},
    )


def test_list_returns_loaded_skills_with_source_path(client):
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()}
    assert set(by_name) == {"full-skill", "minimal-skill"}
    assert by_name["minimal-skill"]["source_path"] == str(FIXTURES / "minimal-skill")
    assert by_name["minimal-skill"]["description"].startswith("The smallest skill")
    assert by_name["minimal-skill"]["bundled"] is False
    assert set(by_name["minimal-skill"]) == {"name", "description", "source_path", "bundled"}


def test_missing_skill_is_not_found(client):
    assert client.get("/api/skills/some-id").status_code == 404


def test_list_marks_the_bundled_library(operator):
    client, _root = operator
    by_name = {s["name"]: s for s in client.get("/api/skills").json()}
    assert by_name[BUNDLED]["bundled"] is True


def test_unset_path_refuses_the_write(tmp_path, monkeypatch):
    missing = tmp_path / "not-created"
    monkeypatch.setattr(
        "core.skills.skill_library.get_settings",
        lambda: Settings(vigil_skills_path=""),
    )
    resp = _write(_app(), "desk-check")
    assert resp.status_code == 400
    assert "unset" in resp.json()["detail"]
    assert not missing.exists()


def test_missing_operator_root_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "missing-root"
    monkeypatch.setattr(
        "core.skills.skill_library.get_settings",
        lambda: Settings(vigil_skills_path=str(missing)),
    )
    resp = _write(_app(), "desk-check")
    assert resp.status_code == 400
    assert not missing.exists()


def test_bundled_name_is_refused_and_the_library_is_unchanged(operator):
    client, root = operator
    before = BUNDLED_FILE.read_bytes()
    resp = _write(client, BUNDLED, description="Should not land.")
    assert resp.status_code == 400
    assert "bundled" in resp.json()["detail"]
    assert BUNDLED_FILE.read_bytes() == before
    assert not (root / BUNDLED).exists()


def test_symlink_out_of_the_operator_root_is_refused(operator):
    client, root = operator
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    resp = _write(client, "escape")
    assert resp.status_code == 400
    assert not (outside / "SKILL.md").exists()


def test_delete_does_not_follow_a_symlink_outside_the_root(operator):
    client, root = operator
    outside = root.parent / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escape\ndescription: Lives outside.\n---\n\n# Outside\n",
        encoding="utf-8",
    )
    (root / "escape").symlink_to(outside)
    resp = client.delete("/api/skills/escape")
    assert resp.status_code == 400
    assert (outside / "SKILL.md").is_file()


def test_symlink_inside_the_root_does_not_overwrite_another_skill(operator):
    client, root = operator
    created = _write(client, "desk-check", description="Keep this text.")
    assert created.status_code == 200
    original = (root / "desk-check" / "SKILL.md").read_bytes()
    (root / "alias").symlink_to(root / "desk-check")
    resp = _write(client, "alias", description="Should not land.")
    assert resp.status_code == 400
    assert (root / "desk-check" / "SKILL.md").read_bytes() == original


def test_tempfile_symlink_is_not_followed(operator):
    client, root = operator
    outside = root.parent / "outside-file"
    outside.write_text("untouched", encoding="utf-8")
    skill_dir = root / "desk-check"
    skill_dir.mkdir()
    (skill_dir / ".SKILL.md.write").symlink_to(outside)
    resp = _write(client, "desk-check")
    assert resp.status_code == 400
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not (skill_dir / "SKILL.md").exists()


def test_write_then_prompt_includes_the_skill(operator):
    client, root = operator
    bundled_before = BUNDLED_FILE.read_bytes()
    description = "Confirm: the next prompt lists a skill just written."
    resp = _write(client, "desk-check", description=description, body="# Desk check\n\nDo the check.\n")
    assert resp.status_code == 200
    assert resp.json()["bundled"] is False
    skill = parse_skill(root / "desk-check")
    assert skill.description == description

    detail = client.get("/api/skills/desk-check")
    assert detail.status_code == 200
    assert detail.json()["body"].startswith("# Desk check")
    assert "name:" not in detail.json()["body"]

    prompt = render_base_prompt(role="Analyst", tools=["read_skill"])
    assert f"- desk-check: {description}" in prompt

    overwritten = "Overwrite the operator skill in place."
    again = _write(client, "desk-check", description=overwritten, body="# Replaced\n")
    assert again.status_code == 200
    assert parse_skill(root / "desk-check").description == overwritten
    assert list(root.iterdir()) == [root / "desk-check"]

    listed = {s["name"]: s for s in client.get("/api/skills").json()}
    assert listed["desk-check"]["bundled"] is False
    assert listed[BUNDLED]["bundled"] is True
    assert BUNDLED_FILE.read_bytes() == bundled_before

    removed = client.delete("/api/skills/desk-check")
    assert removed.status_code == 200
    assert not (root / "desk-check").exists()
    refused = client.delete(f"/api/skills/{BUNDLED}")
    assert refused.status_code == 400
    assert BUNDLED_FILE.is_file()
