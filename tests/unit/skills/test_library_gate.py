"""Every bundled skill ships with an evaluation slice (epic #882 decision 6, #926).

The gate is this test, not a convention: a skill under ``core/skills/library/``
with no ``evals/cases.json``, fewer than three cases, or a case with nothing to
send or nothing to expect fails ``pytest tests/unit/``. ``load_skills`` is not
used here on purpose — it logs and skips an invalid skill, and the gate has to
fail on one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, List

import pytest

from core.skills.skill_library import LIBRARY_ROOT, SkillError, parse_skill

pytestmark = pytest.mark.unit

FIXTURE_LIBRARY = Path(__file__).resolve().parent / "fixtures" / "library"
CASES_FILE = Path("evals") / "cases.json"
MIN_CASES = 3


def _case_problems(index: int, case: Any) -> List[str]:
    if not isinstance(case, dict):
        return [f"case {index} is not an object"]
    problems = []
    name = case.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"case {index} has no `name`")
    label = (
        f"case {name!r}" if isinstance(name, str) and name.strip() else f"case {index}"
    )
    user_input = case.get("input")
    if isinstance(user_input, str):
        if not user_input.strip():
            problems.append(f"{label} has an empty `input`")
    elif not (isinstance(user_input, dict) and user_input):
        problems.append(f"{label} `input` must be a non-empty string or object")
    expect = case.get("expect")
    if (
        not isinstance(expect, list)
        or not expect
        or not all(isinstance(s, str) and s.strip() for s in expect)
    ):
        problems.append(
            f"{label} `expect` must be a non-empty list of non-empty strings"
        )
    return problems


def check_library(root: Path) -> List[str]:
    """Every problem with the skills under ``root``; empty when the library is clean."""
    problems: List[str] = []
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            parse_skill(skill_dir)
        except SkillError as exc:
            problems.append(f"{skill_dir.name}: invalid SKILL.md: {exc}")
            continue
        cases_path = skill_dir / CASES_FILE
        if not cases_path.is_file():
            problems.append(f"{skill_dir.name}: no {CASES_FILE}")
            continue
        try:
            cases = json.loads(cases_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            problems.append(f"{skill_dir.name}: {CASES_FILE} is not valid JSON: {exc}")
            continue
        if not isinstance(cases, list):
            problems.append(f"{skill_dir.name}: {CASES_FILE} must be a list of cases")
            continue
        if len(cases) < MIN_CASES:
            problems.append(
                f"{skill_dir.name}: {len(cases)} cases, need at least {MIN_CASES}"
            )
        for index, case in enumerate(cases):
            problems.extend(
                f"{skill_dir.name}: {p}" for p in _case_problems(index, case)
            )
    return problems


def test_every_bundled_skill_carries_eval_cases():
    assert check_library(LIBRARY_ROOT) == []


def test_the_fixture_library_passes_and_an_empty_root_passes(tmp_path):
    assert check_library(FIXTURE_LIBRARY) == []
    assert check_library(tmp_path) == []


def _copy_fixture(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE_LIBRARY, tmp_path / "library")
    return tmp_path / "library" / "evals-skill"


def _write_cases(skill_dir: Path, cases: Any) -> None:
    (skill_dir / CASES_FILE).write_text(json.dumps(cases))


def _valid_cases(count: int = MIN_CASES) -> List[dict]:
    return [{"name": f"c{i}", "input": "hello", "expect": ["hi"]} for i in range(count)]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: shutil.rmtree(d / "evals"), "no evals/cases.json"),
        (lambda d: _write_cases(d, _valid_cases(2)), "2 cases, need at least 3"),
        (lambda d: _write_cases(d, {"cases": _valid_cases()}), "must be a list"),
        (
            lambda d: _write_cases(
                d, _valid_cases()[:2] + [{**_valid_cases()[0], "input": ""}]
            ),
            "empty `input`",
        ),
        (
            lambda d: _write_cases(
                d, _valid_cases()[:2] + [{**_valid_cases()[0], "expect": []}]
            ),
            "`expect` must be a non-empty list",
        ),
        (
            lambda d: _write_cases(
                d, _valid_cases()[:2] + [{**_valid_cases()[0], "expect": [""]}]
            ),
            "`expect` must be a non-empty list",
        ),
        (
            lambda d: _write_cases(
                d, _valid_cases()[:2] + [{"input": "x", "expect": ["y"]}]
            ),
            "has no `name`",
        ),
        (lambda d: (d / "SKILL.md").write_text("no frontmatter\n"), "invalid SKILL.md"),
    ],
)
def test_the_gate_fails_on_a_broken_skill(tmp_path, mutate, expected):
    skill_dir = _copy_fixture(tmp_path)
    mutate(skill_dir)
    problems = check_library(skill_dir.parent)
    assert problems, "expected the gate to fail"
    assert any(expected in p for p in problems), problems
