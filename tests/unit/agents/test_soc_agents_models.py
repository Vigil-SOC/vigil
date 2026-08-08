"""Unit tests for AgentProfile model/component_category fields (GH #89)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.builtins import (  # noqa: E402
    BUILTIN_AGENTS,
    ORCHESTRATION_DECISION_ID,
    AgentId,
    AgentProfile,
)
from core.agents.manager import SOCAgentLibrary  # noqa: E402

pytestmark = pytest.mark.unit


def test_registry_covers_every_agent_exactly_once():
    """AgentId and the BUILTIN_AGENTS records are one registry in two pieces
    (#476) — adding an agent to one but not the other is the drift this
    issue exists to prevent. Every record must also carry its action id."""
    ids = {a.value for a in AgentId}
    assert {r["id"] for r in BUILTIN_AGENTS} == ids
    assert all(r.get("decision_id") for r in BUILTIN_AGENTS)


def test_decision_ids_are_unique_and_distinct_from_orchestration():
    """Decision ids key the AI-Decisions filter, so a collision would silently
    merge two agents' decisions into one bucket."""
    decision_ids = [r["decision_id"] for r in BUILTIN_AGENTS]
    assert len(set(decision_ids)) == len(decision_ids)
    assert ORCHESTRATION_DECISION_ID not in decision_ids


def test_component_categories_are_valid_model_registry_components():
    from core.llm.providers.registry import is_valid_component

    for record in BUILTIN_AGENTS:
        assert is_valid_component(record["component_category"]), record["id"]


def test_agent_profile_has_new_fields_with_safe_defaults():
    p = AgentProfile(
        id="x",
        name="x",
        description="x",
        system_prompt="x",
        icon="x",
        color="#000",
        specialization="x",
        recommended_tools=[],
    )
    assert p.model is None
    assert p.component_category == "investigation"


def test_builtin_categories_cover_all_built_ins():
    expected_categories = {r["id"]: r["component_category"] for r in BUILTIN_AGENTS}
    agents = SOCAgentLibrary.get_all_agents()
    for agent_id, agent in agents.items():
        assert agent.component_category in {
            "triage",
            "investigation",
            "reporting",
        }, f"{agent_id} has an invalid category: {agent.component_category}"
        assert agent.component_category == expected_categories[agent_id]


def test_triage_agent_is_categorized_as_triage():
    agents = SOCAgentLibrary.get_all_agents()
    assert agents["triage"].component_category == "triage"


def test_reporter_agent_is_categorized_as_reporting():
    agents = SOCAgentLibrary.get_all_agents()
    assert agents["reporter"].component_category == "reporting"


def test_custom_agent_builder_reads_model_and_category():
    row = {
        "id": "custom-test",
        "name": "Custom Test",
        "description": "",
        "role": "tester",
        "recommended_tools": [],
        "max_tokens": 4096,
        "enable_thinking": False,
        "model": "claude-haiku-4-5-20251001",
        "component_category": "triage",
    }
    profile = SOCAgentLibrary.build_profile(row)
    assert profile.model == "claude-haiku-4-5-20251001"
    assert profile.component_category == "triage"


def test_custom_agent_builder_defaults_category_when_missing():
    row = {
        "id": "custom-default",
        "name": "Custom Default",
        "role": "tester",
        "recommended_tools": [],
    }
    profile = SOCAgentLibrary.build_profile(row)
    assert profile.model is None
    assert profile.component_category == "investigation"
