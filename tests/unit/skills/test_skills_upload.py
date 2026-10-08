"""``POST /api/skills/upload`` installs a SKILL.md or a skill zip under the operator root.

Each refusal also checks that the root is left exactly as it was: no partial
skill and no leftover scratch directory.
"""

from __future__ import annotations

import io
import stat
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import Settings
from core.skills import skill_library
from core.skills.skill_library import read_skill
from services.api.routers import skills as skills_router

pytestmark = pytest.mark.unit

BUNDLED = "phishing-triage"


def _skill_md(name: str = "desk-check", extra: str = "") -> str:
    return (
        f"---\nname: {name}\ndescription: Does a desk check.\n{extra}---\n\n# Steps\n"
    )


def _zip(entries: dict[str, str | bytes], symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
        for path in symlinks:
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


@pytest.fixture()
def operator(tmp_path, monkeypatch):
    root = tmp_path / "operator"
    root.mkdir()
    settings = Settings(vigil_skills_path=str(root))
    monkeypatch.setattr("core.skills.skill_library.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/skills")
    return TestClient(app), root


def _upload(client: TestClient, filename: str, data: bytes | str):
    if isinstance(data, str):
        data = data.encode()
    return client.post("/api/skills/upload", files={"file": (filename, data)})


def _refused(resp, status: int, fragment: str, root) -> None:
    assert resp.status_code == status, resp.text
    assert fragment in resp.json()["detail"]
    assert {p.name for p in root.iterdir()} <= {"existing"}  # no skill, no scratch dir


def test_bare_skill_md_installs_and_is_readable(operator):
    client, root = operator
    resp = _upload(client, "SKILL.md", _skill_md())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["name"], body["bundled"], body["file_count"]) == (
        "desk-check",
        False,
        1,
    )
    assert (root / "desk-check" / "SKILL.md").is_file()
    assert read_skill("desk-check")["content"].startswith("# Steps")
    assert [p.name for p in root.iterdir()] == ["desk-check"]


def test_zip_with_references_at_root_installs(operator):
    client, root = operator
    data = _zip(
        {
            "SKILL.md": _skill_md(extra="license: MIT\n"),
            "references/guide.md": "guide",
            "scripts/run.sh": "echo hi",
            ".DS_Store": "junk",
            "__MACOSX/._SKILL.md": "junk",
        }
    )
    resp = _upload(client, "desk-check.zip", data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_count"] == 3
    assert (root / "desk-check" / "references" / "guide.md").read_text() == "guide"
    assert not (root / "desk-check" / ".DS_Store").exists()
    assert "license: MIT" in (root / "desk-check" / "SKILL.md").read_text()


def test_zip_with_one_top_level_folder_installs(operator):
    client, root = operator
    data = _zip({"desk-check/SKILL.md": _skill_md(), "desk-check/references/a.md": "a"})
    resp = _upload(client, "desk-check.zip", data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_count"] == 2
    assert (root / "desk-check" / "references" / "a.md").is_file()


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (_skill_md("Bad_Name"), "must be lowercase letters"),
        ("---\nname: desk-check\n---\n", "`description` must be a non-empty string"),
        ("# no frontmatter\n", "missing YAML frontmatter"),
        (
            _skill_md(extra="metadata:\n  version: 2\n"),
            "`metadata` must be a map of string to string",
        ),
    ],
)
def test_bare_file_breaking_a_loader_rule_is_refused(operator, content, fragment):
    client, root = operator
    _refused(_upload(client, "SKILL.md", content), 400, fragment, root)


def test_zip_folder_name_must_match_the_frontmatter(operator):
    client, root = operator
    data = _zip({"other-name/SKILL.md": _skill_md()})
    _refused(_upload(client, "x.zip", data), 400, "does not match directory", root)


def test_zip_without_a_root_skill_md_is_refused(operator):
    client, root = operator
    data = _zip({"a/SKILL.md": _skill_md("a"), "b/readme.md": "x"})
    _refused(_upload(client, "x.zip", data), 400, "no SKILL.md at the root", root)


def test_bundled_name_is_a_conflict(operator):
    client, root = operator
    _refused(_upload(client, "SKILL.md", _skill_md(BUNDLED)), 409, "bundled", root)


def test_existing_operator_skill_is_a_conflict_and_untouched(operator):
    client, root = operator
    existing = root / "existing"
    existing.mkdir()
    (existing / "SKILL.md").write_text(_skill_md("existing"))
    resp = _upload(client, "SKILL.md", _skill_md("existing").replace("desk", "other"))
    _refused(resp, 409, "Delete it first", root)
    assert "Does a desk check" in (existing / "SKILL.md").read_text()


@pytest.mark.parametrize(
    "entry", ["../x/SKILL.md", "/abs/SKILL.md", "a\\b\\SKILL.md", "C:/SKILL.md"]
)
def test_unsafe_zip_paths_are_refused(operator, entry):
    client, root = operator
    data = _zip({"SKILL.md": _skill_md(), entry: "x"})
    _refused(_upload(client, "x.zip", data), 400, "unsafe path", root)


def test_symlink_entry_is_refused(operator):
    client, root = operator
    data = _zip({"SKILL.md": _skill_md()}, symlinks=("link",))
    _refused(_upload(client, "x.zip", data), 400, "symlink", root)


def test_too_many_files_is_refused(operator, monkeypatch):
    client, root = operator
    monkeypatch.setattr(skill_library, "_FILES_MAX", 3)
    entries = {"SKILL.md": _skill_md(), **{f"r/{i}.md": "x" for i in range(3)}}
    _refused(_upload(client, "x.zip", _zip(entries)), 400, "more than 3 files", root)


def test_oversized_unpacked_zip_is_refused(operator, monkeypatch):
    client, root = operator
    monkeypatch.setattr(skill_library, "_UNPACKED_MAX", 1024)
    data = _zip({"SKILL.md": _skill_md(), "big.txt": "a" * 5000})
    _refused(_upload(client, "x.zip", data), 400, "unpacks to more than", root)


def test_oversized_upload_is_refused(operator, monkeypatch):
    client, root = operator
    monkeypatch.setattr(skill_library, "UPLOAD_MAX", 100)
    monkeypatch.setattr(skills_router, "UPLOAD_MAX", 100)
    _refused(
        _upload(client, "SKILL.md", _skill_md() + "x" * 200), 400, "larger than", root
    )


def test_name_with_a_trailing_newline_is_refused(operator):
    client, root = operator
    resp = _upload(
        client, "SKILL.md", "---\nname: |\n  desk-check\ndescription: x\n---\n"
    )
    _refused(resp, 400, "must be lowercase letters", root)


def test_corrupt_compressed_entry_is_refused_not_a_500(operator):
    client, root = operator
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_BZIP2) as zf:
        zf.writestr("SKILL.md", _skill_md() * 20)
    data = bytearray(buf.getvalue())
    for i in range(45, 70):  # inside the first entry's bzip2 stream
        data[i] ^= 0xFF
    _refused(_upload(client, "x.zip", bytes(data)), 400, "not a readable zip", root)


def test_non_zip_bytes_named_zip_are_refused(operator):
    client, root = operator
    _refused(_upload(client, "x.zip", b"not a zip"), 400, "not a readable zip", root)


def test_unset_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.skills.skill_library.get_settings",
        lambda: Settings(vigil_skills_path=""),
    )
    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/skills")
    resp = _upload(TestClient(app), "SKILL.md", _skill_md())
    assert resp.status_code == 400
    assert "unset" in resp.json()["detail"]
