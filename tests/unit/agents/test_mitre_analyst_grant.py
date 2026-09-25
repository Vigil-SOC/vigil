"""MITRE Analyst holds coverage, execute, and reconstruction (#837)."""

from __future__ import annotations

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.prompts import prompt_for_row

pytestmark = pytest.mark.unit

_GRANTED = (
    "get_finding",
    "get_technique_rollup",
    "recall_entity",
    "atomic_red_team_execute",
    "identify_gaps",
    "analyze_coverage",
    "list_findings",
    "reconstruct_run",
    "check_detection_candidate",
)


def _mitre():
    return next(row for row in BUILTIN_AGENTS if row["id"] == "mitre_analyst")


def test_mitre_analyst_recommended_tools_include_the_coverage_loop():
    tools = _mitre()["recommended_tools"]
    for name in _GRANTED:
        assert name in tools, f"mitre_analyst is missing {name}"


def test_mitre_analyst_prompt_states_the_gated_loop_without_a_phase_order():
    prompt = prompt_for_row(_mitre())
    assert "environment_id" in prompt
    assert "assess coverage" in prompt
    assert "gated tool" in prompt
    assert "reconstruct" in prompt
    assert "report" in prompt
    assert "check_detection_candidate" in prompt
    # Prose, not a numbered phase schedule.
    assert "1. Retrieve findings" not in prompt
