"""Unit tests for the PR-D mechanical optimizations (GH #84).

Covers four small helpers introduced to drive down token spend:
  1. ``ClaudeService._filter_tools_by_name`` — per-agent tool filtering
  2. ``ClaudeService._apply_history_window`` — sliding-window conversation history
  3. ``ClaudeService._truncate_tool_response`` + ``TOOL_RESPONSE_BUDGETS`` — tiered tool-result truncation
  4. ``AgentProfile.thinking_budget`` — per-agent extended-thinking budgets
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Per-agent tool filtering
# ---------------------------------------------------------------------------


class TestFilterToolsByName:
    def test_none_recommended_is_noop(self):
        from services.claude_service import ClaudeService

        tools = [{"name": "a"}, {"name": "b"}]
        assert ClaudeService._filter_tools_by_name(tools, None) is tools

    def test_empty_recommended_is_noop(self):
        from services.claude_service import ClaudeService

        tools = [{"name": "a"}, {"name": "b"}]
        # Empty list returns the original (falsy guard).
        assert ClaudeService._filter_tools_by_name(tools, []) is tools

    def test_exact_name_match(self):
        from services.claude_service import ClaudeService

        tools = [{"name": "get_finding"}, {"name": "create_case"}, {"name": "list_findings"}]
        out = ClaudeService._filter_tools_by_name(
            tools, ["get_finding", "list_findings"]
        )
        assert [t["name"] for t in out] == ["get_finding", "list_findings"]

    def test_mcp_prefixed_name_matches_bare_recommendation(self):
        """Tools arrive as ``<server>_<tool_name>`` from the MCP layer but
        recommended_tools in soc_agents.py use bare names. The filter must
        match both forms so agents don't accidentally ship an empty tool
        block.
        """
        from services.claude_service import ClaudeService

        tools = [
            {"name": "vt_get_file_report"},
            {"name": "splunk_search"},
            {"name": "get_finding"},  # backend tool — already bare
        ]
        out = ClaudeService._filter_tools_by_name(
            tools, ["get_file_report", "get_finding"]
        )
        names = [t["name"] for t in out]
        assert "vt_get_file_report" in names  # matched via strip-prefix
        assert "get_finding" in names  # matched directly
        assert "splunk_search" not in names

    def test_unknown_name_is_dropped(self):
        from services.claude_service import ClaudeService

        tools = [{"name": "a"}, {"name": "b"}]
        assert ClaudeService._filter_tools_by_name(tools, ["c"]) == []


# ---------------------------------------------------------------------------
# 2. Sliding-window history
# ---------------------------------------------------------------------------


class TestApplyHistoryWindow:
    def test_short_history_untouched(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "20")
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        assert ClaudeService._apply_history_window(msgs) == msgs

    def test_long_history_trimmed_to_tail(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "3")
        # 3 turns = 6 messages; build 10 messages and confirm the last 6 win.
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        out = ClaudeService._apply_history_window(msgs)
        assert len(out) == 6
        assert out[0]["content"] == "m4"
        assert out[-1]["content"] == "m9"

    def test_zero_disables_window(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "0")
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        assert ClaudeService._apply_history_window(msgs) == msgs

    def test_bad_env_value_falls_back_to_default(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "not-a-number")
        # Default is 20 turns = 40 messages; 30 messages fits untrimmed.
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        assert ClaudeService._apply_history_window(msgs) == msgs


# ---------------------------------------------------------------------------
# 3. Tiered tool-result truncation
# ---------------------------------------------------------------------------


class TestTieredTruncation:
    def test_per_tool_override_wins_over_default(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("TOOL_RESPONSE_BUDGET_DEFAULT", "1000")
        assert ClaudeService._response_budget_for("get_raw_logs") == 30000
        assert ClaudeService._response_budget_for("list_findings") == 12000
        # Unknown tool falls through to env default.
        assert ClaudeService._response_budget_for("never_heard_of_it") == 1000

    def test_mcp_prefixed_lookup(self):
        from services.claude_service import ClaudeService

        # "splunk_search" is registered; "logs_splunk_search" (prefixed by
        # server name) must still resolve to the same budget.
        assert ClaudeService._response_budget_for("logs_splunk_search") == 30000

    def test_no_name_falls_through_to_default(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.setenv("TOOL_RESPONSE_BUDGET_DEFAULT", "5000")
        assert ClaudeService._response_budget_for(None) == 5000
        assert ClaudeService._response_budget_for("") == 5000

    def test_hard_default_when_env_missing(self, monkeypatch):
        from services.claude_service import ClaudeService

        monkeypatch.delenv("TOOL_RESPONSE_BUDGET_DEFAULT", raising=False)
        assert ClaudeService._response_budget_for("unknown") == 8000

    def test_truncate_respects_per_tool_budget(self, monkeypatch):
        """Regression: previously everything used the 30k constant."""
        from services.claude_service import ClaudeService

        monkeypatch.setenv("TOOL_RESPONSE_BUDGET_DEFAULT", "100")
        svc = ClaudeService.__new__(ClaudeService)
        # content encodes roughly (len // 4) tokens. 1200 chars ≈ 300 tokens,
        # which exceeds 100 tokens (default) and will be truncated.
        content = "x" * 1200
        out = svc._truncate_tool_response(content, tool_name="unknown_tool")
        assert "[TRUNCATED" in out
        # But an overridden tool (get_raw_logs → 30k) keeps the full body.
        full = svc._truncate_tool_response(content, tool_name="get_raw_logs")
        assert full == content


# ---------------------------------------------------------------------------
# 4. Per-agent thinking_budget
# ---------------------------------------------------------------------------


class TestAgentProfileThinkingBudget:
    def test_thinking_agent_has_budget(self):
        from services.soc_agents import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        # Investigator is a deep-reasoning agent — should have a budget set.
        profile = agents["investigator"]
        assert profile.enable_thinking is True
        assert profile.thinking_budget is not None
        assert profile.thinking_budget >= 4000

    def test_auto_responder_budget_is_trimmed(self):
        """auto_responder runs high-confidence pre-approved actions —
        budget should be deliberately small to prevent cost drift."""
        from services.soc_agents import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        profile = agents["auto_responder"]
        assert profile.enable_thinking is True
        assert profile.thinking_budget is not None
        assert profile.thinking_budget <= 5000

    def test_non_thinking_agent_has_no_budget(self):
        """Agents with thinking disabled shouldn't carry a budget."""
        from services.soc_agents import SOCAgentLibrary

        agents = SOCAgentLibrary.get_all_agents()
        profile = agents["triage"]
        assert profile.enable_thinking is False
        assert profile.thinking_budget is None

    def test_custom_agent_picks_up_budget_from_row(self):
        from services.soc_agents import SOCAgentLibrary

        profile = SOCAgentLibrary._build_from_custom(
            {
                "id": "custom-1",
                "name": "Custom",
                "enable_thinking": True,
                "thinking_budget": 4096,
            }
        )
        assert profile.thinking_budget == 4096


# ---------------------------------------------------------------------------
# 5. Daemon agent_runner default budget
# ---------------------------------------------------------------------------


class TestDaemonDefaultThinkingBudget:
    def test_default_value(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_THINKING_BUDGET", raising=False)
        from daemon.agent_runner import _default_thinking_budget

        assert _default_thinking_budget() == 10000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_THINKING_BUDGET", "4096")
        from daemon.agent_runner import _default_thinking_budget

        assert _default_thinking_budget() == 4096

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_THINKING_BUDGET", "not-a-number")
        from daemon.agent_runner import _default_thinking_budget

        assert _default_thinking_budget() == 10000
