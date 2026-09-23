"""The `phishing-triage` skill is the email judgment triage does not hold
(epic #882 slice 2, #1076)."""

from __future__ import annotations

import json

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.prompts import prompt_for_row
from core.skills.skill_library import LIBRARY_ROOT, read_skill

pytestmark = pytest.mark.unit

TRIAGE = {r["id"]: r for r in BUILTIN_AGENTS}["triage"]
CASES_PATH = LIBRARY_ROOT / "phishing-triage" / "evals" / "cases.json"


def test_read_skill_returns_the_body_without_frontmatter():
    result = read_skill("phishing-triage", roots=[LIBRARY_ROOT])
    body = result["content"]
    assert result["skill"] == "phishing-triage"
    assert not body.startswith("---")
    assert "ioc-enrichment" in body
    assert "create_approval_action" in body
    assert "not queried" in body


def test_triage_prompt_lists_the_skill():
    assert "read_skill" in TRIAGE["recommended_tools"]
    prompt = prompt_for_row(TRIAGE)
    assert "- phishing-triage:" in prompt


def test_expect_lines_are_absent_from_each_input():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert len(cases) >= 3
    for case in cases:
        serialized = json.dumps(case["input"])
        for line in case["expect"]:
            assert line not in serialized, case["name"]
