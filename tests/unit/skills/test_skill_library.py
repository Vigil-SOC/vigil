"""The SKILL.md loader and the read_skill tool body (#925)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.config import Settings
from core.skills.skill_library import (
    LIBRARY_ROOT,
    SkillError,
    load_skills,
    parse_skill,
    read_skill,
    skill_roots,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _write_skill(root: Path, dirname: str, frontmatter: str, body: str = "Body.\n"):
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}")
    return skill_dir


def test_fixture_root_loads_the_valid_skills_and_skips_the_mismatch(caplog):
    with caplog.at_level(logging.WARNING, logger="core.skills.skill_library"):
        skills = {s.name: s for s in load_skills([FIXTURES])}

    assert set(skills) == {"minimal-skill", "full-skill"}
    assert skills["minimal-skill"].path == FIXTURES / "minimal-skill"
    assert skills["minimal-skill"].description.startswith("The smallest skill")
    assert "mismatched-dir" in caplog.text
    assert "does not match directory" in caplog.text


def test_every_optional_spec_field_is_accepted():
    skill = parse_skill(FIXTURES / "full-skill")
    assert skill.name == "full-skill"


@pytest.mark.parametrize(
    "dirname, frontmatter, reason",
    [
        ("Bad-Case", "name: Bad-Case\ndescription: d", "lowercase"),
        ("-lead", "name: -lead\ndescription: d", "hyphen"),
        ("dbl--hyphen", "name: dbl--hyphen\ndescription: d", "hyphen"),
        ("a" * 65, f"name: {'a' * 65}\ndescription: d", "64 characters"),
        ("no-desc", "name: no-desc", "`description`"),
        ("long-desc", f"name: long-desc\ndescription: {'x' * 1025}", "1024"),
        ("bad-meta", "name: bad-meta\ndescription: d\nmetadata:\n  n: 1", "`metadata`"),
        ("bad-tools", "name: bad-tools\ndescription: d\nallowed-tools: [a]", "`allowed-tools`"),
    ],
)
def test_spec_violations_are_rejected(tmp_path, dirname, frontmatter, reason):
    skill_dir = _write_skill(tmp_path, dirname, frontmatter)
    with pytest.raises(SkillError, match=reason):
        parse_skill(skill_dir)


def test_a_directory_without_frontmatter_or_skill_file_is_skipped(tmp_path):
    (tmp_path / "no-file").mkdir()
    (tmp_path / "no-front").mkdir()
    (tmp_path / "no-front" / "SKILL.md").write_text("# just a body\n")
    (tmp_path / "loose.md").write_text("not a directory")
    assert load_skills([tmp_path]) == []


def test_earlier_root_wins_a_name_clash_and_missing_roots_are_fine(tmp_path):
    first = _write_skill(tmp_path / "one", "dup", "name: dup\ndescription: first")
    _write_skill(tmp_path / "two", "dup", "name: dup\ndescription: second")
    skills = load_skills([tmp_path / "one", tmp_path / "two", tmp_path / "absent"])
    assert [s.path for s in skills] == [first]


def test_skill_roots_is_the_library_then_the_setting():
    assert skill_roots(Settings(vigil_skills_path="")) == [LIBRARY_ROOT]
    assert skill_roots(Settings(vigil_skills_path="/opt/skills")) == [
        LIBRARY_ROOT,
        Path("/opt/skills"),
    ]


def test_read_skill_returns_the_body_without_frontmatter():
    result = read_skill("minimal-skill", roots=[FIXTURES])
    assert result["file"] == "SKILL.md"
    assert result["content"].startswith("# Minimal skill")
    assert "name: minimal-skill" not in result["content"]


def test_read_skill_returns_a_references_file():
    result = read_skill("full-skill", "references/checklist.md", roots=[FIXTURES])
    assert result["file"] == "references/checklist.md"
    assert "Source of the alert identified" in result["content"]


@pytest.mark.parametrize(
    "file",
    [
        "../minimal-skill/SKILL.md",
        "references/../../minimal-skill/SKILL.md",
        "/etc/hostname",
        str(FIXTURES / "minimal-skill" / "SKILL.md"),
        "C:\\windows\\win.ini",
    ],
)
def test_read_skill_refuses_a_path_outside_the_skill(file):
    result = read_skill("full-skill", file, roots=[FIXTURES])
    assert set(result) == {"error"}
    assert "outside skill" in result["error"]


def test_read_skill_refuses_a_symlink_escaping_the_skill(tmp_path):
    skill_dir = _write_skill(tmp_path / "root", "linky", "name: linky\ndescription: d")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (skill_dir / "escape.txt").symlink_to(secret)
    result = read_skill("linky", "escape.txt", roots=[tmp_path / "root"])
    assert "outside skill" in result["error"]


def test_read_skill_reports_unknown_skill_file_and_missing_name():
    assert "No skill" in read_skill("nope", roots=[FIXTURES])["error"]
    assert "no file" in read_skill("full-skill", "references/x.md", roots=[FIXTURES])["error"]
    assert "requires" in read_skill(None, roots=[FIXTURES])["error"]
    # The mismatched fixture is on disk but was never loaded, so it is unreadable.
    assert "No skill" in read_skill("some-other-name", roots=[FIXTURES])["error"]
