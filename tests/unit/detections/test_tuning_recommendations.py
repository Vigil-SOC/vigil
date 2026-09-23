"""Unit tests for the tuning-recommendations math (GH #84 PR-E follow-up).

``scripts/compute_tuning_recommendations.py`` reads LLMInteractionLog
rows and produces p50 / p95 / max stats plus rounded recommendations.
The DB read is exercised in integration; these tests pin the pure-math
helpers so a refactor can't silently skew recommendations.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO / "scripts" / "compute_tuning_recommendations.py"


def _load_script():
    """Load compute_tuning_recommendations.py as an isolated module."""
    # The script's module-level sys.path.insert expects REPO already; do it
    # explicitly so the import doesn't blow up under pytest's altered paths.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("tuning_rec_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rec():
    return _load_script()


def _row(
    *,
    agent_id: str | None = "investigator",
    thinking_enabled: bool = True,
    thinking_content: str = "",
) -> dict:
    return {
        "agent_id": agent_id,
        "thinking_enabled": thinking_enabled,
        "thinking_content": thinking_content,
    }


class TestThinkingBudgetRecs:
    def test_empty_rows_returns_empty(self, rec):
        assert rec.recommend_thinking_budgets([]) == {}

    def test_non_thinking_rows_skipped(self, rec):
        rows = [_row(thinking_enabled=False, thinking_content="x" * 4000)]
        assert rec.recommend_thinking_budgets(rows) == {}

    def test_p95_rounded_up_with_5pct_headroom(self, rec):
        """100 rows with thinking tokens 100..10000 in 100-token steps.
        p95 ≈ 9500 tokens. Recommendation: 9500 * 1.05 = 9975 → round to
        nearest 500 = 10000."""
        rows = [
            _row(
                agent_id="investigator",
                thinking_content="x" * (n * 4),  # n tokens
            )
            for n in range(100, 10001, 100)
        ]
        out = rec.recommend_thinking_budgets(rows)
        assert "investigator" in out
        stats = out["investigator"]
        assert stats["samples"] == 100
        assert stats["p95"] == pytest.approx(9500, abs=200)
        assert stats["recommended_thinking_budget"] >= 9500
        # Rounding + headroom keeps it ≤ max + 500.
        assert stats["recommended_thinking_budget"] <= stats["max"] + 500

    def test_floor_at_1000(self, rec):
        """An agent that only ever uses 20 thinking tokens shouldn't get
        a sub-1k budget (safety floor for rare complex prompts)."""
        rows = [_row(thinking_content="x" * 80) for _ in range(30)]
        out = rec.recommend_thinking_budgets(rows)
        assert out["investigator"]["recommended_thinking_budget"] >= 1000

    def test_per_agent_grouping(self, rec):
        rows = [
            _row(agent_id="investigator", thinking_content="x" * 40000),
            _row(agent_id="triage", thinking_content="x" * 4000),
            _row(agent_id="triage", thinking_content="x" * 6000),
        ]
        out = rec.recommend_thinking_budgets(rows)
        assert set(out.keys()) == {"investigator", "triage"}
        # Triage's recommendation should be lower than investigator's.
        assert (
            out["triage"]["recommended_thinking_budget"]
            < out["investigator"]["recommended_thinking_budget"]
        )


def test_script_does_not_recommend_removed_ai_operations_knobs():
    """The daemon-wide budget, history window, and tool-result budget are
    not settings anymore. Recommending them would send operators to knobs
    that do nothing."""
    text = SCRIPT.read_text()
    for name in (
        "recommend_history_window",
        "recommend_tool_response_budget",
        "recommend_daemon_thinking_budget",
        "CLAUDE_HISTORY_WINDOW",
        "TOOL_RESPONSE_BUDGET_DEFAULT",
        "CLAUDE_THINKING_BUDGET",
        "Settings → AI Config",
    ):
        assert name not in text


def test_script_does_not_tell_operators_to_set_database_url():
    """Usage used to export DATABASE_URL. The script connects through
    get_session(), which ignores that variable (#752)."""
    text = SCRIPT.read_text()
    assert "DATABASE_URL=" not in text
    assert "Check DATABASE_URL" not in text
    assert "get_session()" in text
