"""Unit tests for LLM cost analytics aggregation (GH #84 PR-A).

Covers the pure-function helpers in ``backend.api.analytics`` that back
``GET /analytics/cost``. Aggregation SQL against a real session is
exercised in the integration suite; here we assert the shape of
``_cache_hit_rate`` and the endpoint response envelope.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


class TestCacheHitRate:
    def test_returns_zero_when_no_tokens(self):
        from backend.api.analytics import _cache_hit_rate

        assert _cache_hit_rate(0, 0) == 0.0

    def test_returns_zero_when_no_cache_reads(self):
        from backend.api.analytics import _cache_hit_rate

        assert _cache_hit_rate(1000, 0) == 0.0

    def test_full_cache_hit(self):
        from backend.api.analytics import _cache_hit_rate

        assert _cache_hit_rate(0, 1000) == 1.0

    def test_half_cached(self):
        from backend.api.analytics import _cache_hit_rate

        # 500 new + 500 cached → 50% hit
        assert _cache_hit_rate(500, 500) == 0.5

    def test_rounds_to_four_decimals(self):
        from backend.api.analytics import _cache_hit_rate

        # 1/3 cached — ensure deterministic rounding, not full float precision
        rate = _cache_hit_rate(2, 1)
        assert rate == 0.3333


@pytest.mark.asyncio
async def test_get_cost_analytics_response_shape(monkeypatch):
    """The endpoint must return the envelope the frontend expects.

    Mocks the four aggregation helpers to isolate the response shape from
    SQL — we care that ``window``, ``totals``, ``by_agent``, ``by_model``,
    and ``top_investigations`` are all present and JSON-serializable.
    """
    from backend.api import analytics as mod

    fake_totals = {
        "calls": 3,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 25,
        "cache_creation_tokens": 10,
        "cost_usd": 0.01,
        "cache_hit_rate": 0.2,
    }
    fake_agents = [{"agent_id": "triage", "calls": 3, "cost_usd": 0.01}]
    fake_models = [{"model": "claude-sonnet-4-5", "calls": 3, "cost_usd": 0.01}]
    fake_top = [{"investigation_id": "inv-1", "calls": 3, "cost_usd": 0.01}]

    monkeypatch.setattr(mod, "_cost_totals", lambda db, f: fake_totals)
    monkeypatch.setattr(mod, "_cost_group_by_agent", lambda db, f: fake_agents)
    monkeypatch.setattr(mod, "_cost_group_by_model", lambda db, f: fake_models)
    monkeypatch.setattr(mod, "_cost_top_investigations", lambda db, f: fake_top)

    fake_db = MagicMock()
    result = await mod.get_cost_analytics(time_range="24h", db=fake_db)

    assert set(result.keys()) == {
        "window",
        "totals",
        "by_agent",
        "by_model",
        "top_investigations",
    }
    assert result["totals"] == fake_totals
    assert result["by_agent"] == fake_agents
    assert result["by_model"] == fake_models
    assert result["top_investigations"] == fake_top
    # window carries ISO timestamps + a seconds count we can display
    assert "start" in result["window"]
    assert "end" in result["window"]
    assert result["window"]["seconds"] == 24 * 3600
