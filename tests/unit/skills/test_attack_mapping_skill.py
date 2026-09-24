"""The attack-mapping skill grounds technique ids in a finding's observables
(epic #882 slice 2, #1077)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.prompts import prompt_for_row
from core.skills.skill_library import LIBRARY_ROOT, read_skill

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "skill_eval_attack_mapping", REPO / "scripts" / "skill_eval.py"
)
skill_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_eval)

TRIAGE = {row["id"]: row for row in BUILTIN_AGENTS}["triage"]
CASES = json.loads(
    (LIBRARY_ROOT / "attack-mapping" / "evals" / "cases.json").read_text(
        encoding="utf-8"
    )
)
_FORBIDDEN = ("coverage", "gap", "Atomic Red Team", "reconstruct_run")


def test_read_skill_returns_the_body_without_frontmatter():
    result = read_skill("attack-mapping", roots=[LIBRARY_ROOT])
    body = result["content"]
    assert result["skill"] == "attack-mapping"
    assert not body.startswith("---")
    for phrase in ("get_finding", "get_technique_rollup", "not checked", "T1110.001"):
        assert phrase in body
    for phrase in _FORBIDDEN:
        assert phrase not in body


def test_triage_prompt_lists_the_skill():
    assert "read_skill" in TRIAGE["recommended_tools"]
    prompt = prompt_for_row(TRIAGE)
    assert "- attack-mapping:" in prompt


def test_expect_strings_are_absent_from_the_user_turn():
    assert len(CASES) >= 3
    for case in CASES:
        turn = skill_eval.as_user_turn(case["input"])
        for expected in case["expect"]:
            assert (
                expected not in turn
            ), f"{case['name']}: {expected!r} is already in the input"
