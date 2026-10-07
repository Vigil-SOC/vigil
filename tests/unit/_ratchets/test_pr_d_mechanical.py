"""Per-agent thinking_budget on SOCAgentLibrary profiles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


class TestAgentProfileThinkingBudget:
    def test_thinking_agent_has_budget(self):
        from core.agents.manager import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        # Investigator is a deep-reasoning agent — should have a budget set.
        profile = agents["investigator"]
        assert profile.enable_thinking is True
        assert profile.thinking_budget is not None
        assert profile.thinking_budget >= 4000

    def test_auto_responder_budget_is_trimmed(self):
        """auto_responder runs high-confidence pre-approved actions —
        budget should be deliberately small to prevent cost drift."""
        from core.agents.manager import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        profile = agents["auto_responder"]
        assert profile.enable_thinking is True
        assert profile.thinking_budget is not None
        assert profile.thinking_budget <= 5000

    def test_non_thinking_agent_has_no_budget(self):
        """Agents with thinking disabled shouldn't carry a budget."""
        from core.agents.manager import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        profile = agents["triage"]
        assert profile.enable_thinking is False
        assert profile.thinking_budget is None

    def test_custom_agent_picks_up_budget_from_row(self):
        from core.agents.manager import SOCAgentLibrary

        profile = SOCAgentLibrary.build_profile(
            {
                "id": "custom-1",
                "name": "Custom",
                "enable_thinking": True,
                "thinking_budget": 4096,
            }
        )
        assert profile.thinking_budget == 4096
