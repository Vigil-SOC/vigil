"""``/api/skills`` lists skills on disk and writes under the operator root.

The list fixture mounts the router with ``skill_roots`` pointed at the fixture
directory. Write tests use a temp operator root and the real loader, so a
prompt built afterwards reads the file that was just written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
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


def _write(
    client: TestClient,
    name: str,
    description: str = "A saved skill.",
    body: str = "# Saved\n",
    version: int | None = None,
):
    """Create, or overwrite when ``version`` is the one the caller opened."""
    return client.post(
        "/api/skills",
        json={
            "name": name,
            "description": description,
            "body": body,
            **({} if version is None else {"version": version}),
        },
    )


def test_list_returns_loaded_skills_with_source_path(client):
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()}
    assert set(by_name) == {"full-skill", "minimal-skill"}
    assert by_name["minimal-skill"]["source_path"] == str(FIXTURES / "minimal-skill")
    assert by_name["minimal-skill"]["description"].startswith("The smallest skill")
    assert by_name["minimal-skill"]["bundled"] is False
    assert set(by_name["minimal-skill"]) == {
        "name",
        "description",
        "source_path",
        "bundled",
        "file_count",
    }


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
    resp = _write(client, "desk-check", version=1)
    assert resp.status_code == 400
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not (skill_dir / "SKILL.md").exists()


def test_write_then_prompt_includes_the_skill(operator):
    client, root = operator
    bundled_before = BUNDLED_FILE.read_bytes()
    description = "Confirm: the next prompt lists a skill just written."
    resp = _write(
        client,
        "desk-check",
        description=description,
        body="# Desk check\n\nDo the check.\n",
    )
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
    again = _write(
        client, "desk-check", description=overwritten, body="# Replaced\n", version=1
    )
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


FOLDER_SKILL = "executive-summary"  # bundled: evals/, assets/ and metadata.version


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


def test_detail_lists_files_and_version_and_the_list_is_unchanged(operator):
    client, _ = operator
    detail = client.get(f"/api/skills/{FOLDER_SKILL}").json()
    assert detail["version"] == 1
    assert [f["path"] for f in detail["files"]] == [
        "SKILL.md",
        "assets/board-brief.md",
        "evals/cases.json",
    ]
    assert all(f["size"] > 0 for f in detail["files"])
    assert "files" not in client.get("/api/skills").json()[0]


def test_file_count_matches_the_detail_files(operator):
    client, root = operator
    assert _write(client, "solo").status_code == 200
    assert _write(client, "nested").status_code == 200
    (root / "nested" / "scripts").mkdir()
    (root / "nested" / "scripts" / "run.py").write_text("print(1)\n")
    (root / "nested" / ".hidden").write_text("x")
    by_name = {s["name"]: s for s in client.get("/api/skills").json()}
    assert by_name["solo"]["file_count"] == 1
    assert by_name["nested"]["file_count"] == 2
    for name in (BUNDLED, FOLDER_SKILL, "solo", "nested"):
        detail = client.get(f"/api/skills/{name}").json()
        assert detail["file_count"] == len(detail["files"])
        assert by_name[name]["file_count"] == len(detail["files"])
    assert by_name[FOLDER_SKILL]["file_count"] == 3


def test_file_read_returns_text_and_refuses_escapes_and_binaries(operator):
    client, root = operator
    _write(client, "desk-check")
    skill_dir = root / "desk-check"
    (skill_dir / "blob.bin").write_bytes(b"\xff\xfe\x00\x80")
    secret = root.parent / "secret.txt"
    secret.write_text("nope")
    (skill_dir / "escape.txt").symlink_to(secret)

    ok = client.get(f"/api/skills/{FOLDER_SKILL}/files/assets/board-brief.md")
    assert ok.status_code == 200 and ok.json()["content"].strip()
    assert client.get("/api/skills/desk-check/files/blob.bin").status_code == 400
    assert (
        "not a text file"
        in client.get("/api/skills/desk-check/files/blob.bin").json()["detail"]
    )
    assert client.get("/api/skills/desk-check/files/escape.txt").status_code == 400
    assert client.get("/api/skills/desk-check/files/..%2Fsecret.txt").status_code in (
        400,
        404,
    )
    assert client.get("/api/skills/desk-check/files/nope.md").status_code == 404
    assert client.get("/api/skills/nope/files/SKILL.md").status_code == 404
    # symlinks and dotfiles stay out of the list
    paths = [f["path"] for f in client.get("/api/skills/desk-check").json()["files"]]
    assert paths == ["SKILL.md", "blob.bin"]


def test_each_save_adds_one_to_the_version_and_keeps_other_frontmatter(operator):
    client, root = operator
    assert _write(client, "desk-check").status_code == 200
    assert client.get("/api/skills/desk-check").json()["version"] == 1
    skill_md = root / "desk-check" / "SKILL.md"
    skill_md.write_text(
        "---\nname: desk-check\ndescription: Old.\nlicense: MIT\n"
        "compatibility: any\nallowed-tools: Read\nextra-key: kept\n"
        "metadata:\n  vigil-origin: me\n  version: '1'\n---\n\nBody\n"
    )
    (root / "desk-check" / "notes.md").write_text("keep me")
    for opened in (1, 2):
        resp = _write(client, "desk-check", description="New.", version=opened)
        assert resp.status_code == 200
        assert client.get("/api/skills/desk-check").json()["version"] == opened + 1
    fm = _frontmatter(skill_md)
    assert list(fm)[:2] == ["name", "description"]
    assert fm["description"] == "New."
    assert (fm["license"], fm["compatibility"], fm["allowed-tools"]) == (
        "MIT",
        "any",
        "Read",
    )
    assert fm["extra-key"] == "kept"
    assert fm["metadata"] == {"vigil-origin": "me", "version": "3"}
    assert (root / "desk-check" / "notes.md").read_text() == "keep me"


def test_a_skill_without_a_version_counts_as_one_and_saves_as_two(operator):
    client, root = operator
    skill_dir = root / "plain"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: plain\ndescription: d\n---\n\nBody\n"
    )
    assert client.get("/api/skills/plain").json()["version"] == 1
    assert _write(client, "plain", version=1).status_code == 200
    assert client.get("/api/skills/plain").json()["version"] == 2


def test_saving_a_builtin_as_a_copy_carries_the_folder_at_version_one(operator):
    client, root = operator
    before = {
        p: p.read_bytes()
        for p in (LIBRARY_ROOT / FOLDER_SKILL).rglob("*")
        if p.is_file()
    }
    resp = client.post(
        "/api/skills",
        json={
            "name": "my-summary",
            "description": "Mine.",
            "body": "# Mine\n",
            "source": FOLDER_SKILL,
        },
    )
    assert resp.status_code == 200
    copy = root / "my-summary"
    assert (copy / "evals" / "cases.json").read_bytes() == (
        LIBRARY_ROOT / FOLDER_SKILL / "evals" / "cases.json"
    ).read_bytes()
    assert (copy / "assets" / "board-brief.md").is_file()
    assert _frontmatter(copy / "SKILL.md")["metadata"] == {"version": "1"}
    assert client.get("/api/skills/my-summary").json()["version"] == 1
    assert list(root.iterdir()) == [copy]
    assert before == {p: p.read_bytes() for p in before}

    # a second save is a plain overwrite: version 2, files kept
    again = _write(client, "my-summary", description="Mine again.", version=1)
    assert again.status_code == 200
    assert client.get("/api/skills/my-summary").json()["version"] == 2
    assert (copy / "evals" / "cases.json").is_file()


def test_a_copy_refuses_bad_sources_taken_names_and_leaves_nothing_behind(operator):
    client, root = operator
    body = {"description": "Mine.", "body": "", "source": FOLDER_SKILL}
    assert (
        client.post(
            "/api/skills", json={"name": "x", **body, "source": "nope"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/skills", json={"name": "Bad Name", **body}).status_code == 400
    )
    assert (
        client.post("/api/skills", json={"name": FOLDER_SKILL, **body}).status_code
        == 400
    )
    _write(client, "taken")
    assert client.post("/api/skills", json={"name": "taken", **body}).status_code == 400
    assert [p.name for p in root.iterdir()] == ["taken"]


def test_a_stale_version_is_refused_with_409_and_nothing_is_written(operator):
    client, root = operator
    assert _write(client, "desk-check", description="First.").status_code == 200
    assert (
        _write(client, "desk-check", description="Second.", version=1).status_code
        == 200
    )
    skill_md = root / "desk-check" / "SKILL.md"
    before = skill_md.read_bytes()
    stale = _write(client, "desk-check", description="Lost.", version=1)
    assert stale.status_code == 409
    assert stale.json()["detail"] == (
        "This skill changed since you opened it. Reopen it to see the latest."
    )
    assert skill_md.read_bytes() == before
    assert (
        _write(client, "desk-check", description="Third.", version=2).status_code == 200
    )
    assert client.get("/api/skills/desk-check").json()["version"] == 3


def test_a_create_into_an_existing_folder_is_refused_and_leaves_it_alone(operator):
    client, root = operator
    assert _write(client, "desk-check").status_code == 200
    broken = root / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("not a skill", encoding="utf-8")
    empty = root / "empty"
    empty.mkdir()
    for name in ("desk-check", "broken", "empty"):
        before = {p: p.read_bytes() for p in (root / name).rglob("*") if p.is_file()}
        resp = _write(client, name)
        assert resp.status_code == 409
        assert resp.json()["detail"] == f"A skill folder named {name} already exists."
        assert before == {
            p: p.read_bytes() for p in (root / name).rglob("*") if p.is_file()
        }
    assert list((empty).iterdir()) == []


def test_a_copy_leaves_hidden_files_behind_and_lists_what_it_copied(
    tmp_path, monkeypatch
):
    library = tmp_path / "library"
    source = library / "wip"
    (source / "evals").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("x")
    (source / ".env").write_text("secret")
    (source / "evals" / ".hidden").write_text("x")
    (source / "evals" / "cases.json").write_text("[]")
    (source / "SKILL.md").write_text("---\nname: wip\ndescription: d\n---\n\nBody\n")
    (source / "link").symlink_to(source / "evals")
    root = tmp_path / "operator"
    root.mkdir()
    settings = Settings(vigil_skills_path=str(root))
    monkeypatch.setattr("core.skills.skill_library.get_settings", lambda: settings)
    monkeypatch.setattr("core.skills.skill_library.LIBRARY_ROOT", library)
    client = _app()
    listed = client.get("/api/skills/wip").json()["files"]
    resp = client.post(
        "/api/skills",
        json={"name": "wip-copy", "description": "d", "body": "", "source": "wip"},
    )
    assert resp.status_code == 200
    copied = client.get("/api/skills/wip-copy").json()["files"]
    assert [f["path"] for f in copied] == [f["path"] for f in listed]
    assert sorted(p.name for p in (root / "wip-copy").rglob("*")) == sorted(
        ["SKILL.md", "evals", "cases.json"]
    )
    assert [p.name for p in root.iterdir()] == ["wip-copy"]
