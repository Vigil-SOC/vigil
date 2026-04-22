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

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "compute_tuning_recommendations.py"


def _load_script():
    """Load compute_tuning_recommendations.py as an isolated module."""
    # The script's module-level sys.path.insert expects REPO already; do it
    # explicitly so the import doesn't blow up under pytest's altered paths.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(REPO / "backend") not in sys.path:
        sys.path.insert(0, str(REPO / "backend"))
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
    input_tokens: int = 1000,
    output_tokens: int = 200,
    thinking_enabled: bool = True,
    thinking_content: str = "",
    tool_results: list | None = None,
    request_messages: list | None = None,
) -> dict:
    return {
        "agent_id": agent_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_enabled": thinking_enabled,
        "thinking_budget": 10000,
        "thinking_content": thinking_content,
        "tool_results": tool_results or [],
        "request_messages": request_messages or [],
        "cost_usd": 0.01,
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


class TestHistoryWindow:
    def test_reports_distribution(self, rec):
        rows = [
            _row(request_messages=[{"role": "user"}] * n)
            for n in (5, 10, 15, 40, 80)
        ]
        out = rec.recommend_history_window(rows)
        assert out["samples"] == 5
        assert out["p50_messages"] == 15
        assert out["max_messages"] == 80

    def test_no_rows(self, rec):
        assert rec.recommend_history_window([]) == {"samples": 0}


class TestToolResponseBudget:
    def test_reports_and_rounds_to_nearest_1k(self, rec):
        # 10 tool results: sizes in tokens 100, 500, 1000, ..., up to a few k.
        rows = [
            _row(
                tool_results=[
                    {
                        "content": [
                            {"type": "text", "text": "x" * (tokens * 4)},
                        ]
                    }
                ],
            )
            for tokens in [200, 500, 800, 1000, 1500, 2000, 3000, 4000, 6000, 9000]
        ]
        out = rec.recommend_tool_response_budget(rows)
        assert out["samples"] == 10
        # Recommendation rounds p95 (9000) to nearest 1k = 9000.
        assert out["recommended_default"] % 1000 == 0

    def test_no_rows(self, rec):
        assert rec.recommend_tool_response_budget([]) == {"samples": 0}


class TestDaemonThinkingBudget:
    def test_only_rows_without_agent_id_counted(self, rec):
        rows = [
            # Named sub-agent rows — should be skipped by daemon reco.
            _row(agent_id="investigator", thinking_content="x" * 40000),
            # Daemon rows (no agent_id).
            _row(agent_id=None, thinking_content="x" * 2000),
            _row(agent_id=None, thinking_content="x" * 3000),
            _row(agent_id=None, thinking_content="x" * 5000),
        ]
        out = rec.recommend_daemon_thinking_budget(rows)
        assert out["samples"] == 3
        assert out["max"] == 1250  # 5000 chars // 4 = 1250 tokens
        assert out["recommended"] >= 2000  # floor

    def test_no_daemon_rows(self, rec):
        rows = [_row(agent_id="investigator", thinking_content="x" * 40000)]
        assert rec.recommend_daemon_thinking_budget(rows) == {"samples": 0}
