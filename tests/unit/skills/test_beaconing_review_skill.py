"""The beaconing-review skill grades periodic outbound traffic (#882 slice 2, #1074)."""

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
    "skill_eval_beaconing", REPO / "scripts" / "skill_eval.py"
)
skill_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_eval)

NETWORK_ANALYST = {row["id"]: row for row in BUILTIN_AGENTS}["network_analyst"]
CASES = json.loads(
    (LIBRARY_ROOT / "beaconing-review" / "evals" / "cases.json").read_text(
        encoding="utf-8"
    )
)


def test_read_skill_returns_the_body_without_frontmatter():
    result = read_skill("beaconing-review", roots=[LIBRARY_ROOT])
    body = result["content"]
    assert result["skill"] == "beaconing-review"
    assert not body.startswith("---")
    for phrase in (
        "recall_entity",
        "lookup_indicators",
        "cf_lookup_ip_threat",
        "cf_lookup_domain_threat",
        "ioc-enrichment",
        "not checked",
        "benign-periodic",
    ):
        assert phrase in body


def test_network_analyst_prompt_lists_the_skill():
    assert "read_skill" in NETWORK_ANALYST["recommended_tools"]
    prompt = prompt_for_row(NETWORK_ANALYST)
    assert "- beaconing-review:" in prompt


def test_expect_strings_are_absent_from_the_user_turn():
    assert len(CASES) >= 3
    for case in CASES:
        turn = skill_eval.as_user_turn(case["input"])
        for expected in case["expect"]:
            assert (
                expected not in turn
            ), f"{case['name']}: {expected!r} is already in the input"
