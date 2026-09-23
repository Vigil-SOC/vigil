"""The ``rdp-lateral-movement`` skill judges an internal RDP session
(epic #882 slice 2, #1075)."""

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
    "skill_eval", REPO / "scripts" / "skill_eval.py"
)
skill_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_eval)

NETWORK_ANALYST = {r["id"]: r for r in BUILTIN_AGENTS}["network_analyst"]
CASES_PATH = LIBRARY_ROOT / "rdp-lateral-movement" / "evals" / "cases.json"


def test_read_skill_returns_the_body_without_frontmatter():
    result = read_skill("rdp-lateral-movement", roots=[LIBRARY_ROOT])
    body = result["content"]
    assert result["skill"] == "rdp-lateral-movement"
    assert not body.startswith("---")
    for phrase in (
        "get_finding",
        "list_findings",
        "search_findings",
        "recall_entity",
        "stated in this finding",
        "not checked",
        "technique: T1021.001",
        "path: <src> -> <dst> as <account>",
    ):
        assert phrase in body


def test_network_analyst_lists_the_skill():
    prompt = prompt_for_row(NETWORK_ANALYST)
    assert "- rdp-lateral-movement:" in prompt
    assert "read_skill" in NETWORK_ANALYST["recommended_tools"]


def test_every_expect_is_absent_from_the_serialized_input():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert len(cases) >= 3
    for case in cases:
        turn = skill_eval.as_user_turn(case["input"])
        for expected in case["expect"]:
            assert expected not in turn, case["name"]
