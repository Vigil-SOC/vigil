"""Unit tests for AgentProfile model/component_category fields (GH #89)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.soc_agents import (  # noqa: E402
    AgentProfile,
    SOCAgentLibrary,
    _BUILTIN_COMPONENT_CATEGORY,
)

pytestmark = pytest.mark.unit


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
    agents = SOCAgentLibrary.get_all_agents()
    for agent_id, agent in agents.items():
        assert agent.component_category in {
            "triage",
            "investigation",
            "reporting",
        }, f"{agent_id} has an invalid category: {agent.component_category}"
        assert agent.component_category == _BUILTIN_COMPONENT_CATEGORY.get(
            agent_id, "investigation"
        )


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
    profile = SOCAgentLibrary._build_from_custom(row)
    assert profile.model == "claude-haiku-4-5-20251001"
    assert profile.component_category == "triage"


def test_custom_agent_builder_defaults_category_when_missing():
    row = {
        "id": "custom-default",
        "name": "Custom Default",
        "role": "tester",
        "recommended_tools": [],
    }
    profile = SOCAgentLibrary._build_from_custom(row)
    assert profile.model is None
    assert profile.component_category == "investigation"
