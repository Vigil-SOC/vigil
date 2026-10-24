"""Unit tests for LLM cost analytics aggregation (GH #84 PR-A).

Covers the pure-function helpers in ``core.reporting.analytics_service`` that back
``GET /analytics/cost``. Aggregation SQL against a real session is
exercised in the integration suite; here we assert the shape of
``_cache_hit_rate`` and the endpoint response envelope.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


class TestCacheHitRate:
    def test_returns_zero_when_no_tokens(self):
        from core.reporting.analytics_service import _cache_hit_rate

        assert _cache_hit_rate(0, 0) == 0.0

    def test_returns_zero_when_no_cache_reads(self):
        from core.reporting.analytics_service import _cache_hit_rate

        assert _cache_hit_rate(1000, 0) == 0.0

    def test_full_cache_hit(self):
        from core.reporting.analytics_service import _cache_hit_rate

        assert _cache_hit_rate(0, 1000) == 1.0

    def test_half_cached(self):
        from core.reporting.analytics_service import _cache_hit_rate

        # 500 new + 500 cached → 50% hit
        assert _cache_hit_rate(500, 500) == 0.5

    def test_rounds_to_four_decimals(self):
        from core.reporting.analytics_service import _cache_hit_rate

        # 1/3 cached — ensure deterministic rounding, not full float precision
        rate = _cache_hit_rate(2, 1)
        assert rate == 0.3333


@pytest.mark.asyncio
async def test_get_cost_analytics_response_shape(monkeypatch):
    import httpx

    from core.reporting import analytics_service as svc
    from services.api.routers import analytics as mod

    bifrost_calls = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: bifrost_calls.append(a))
    fake_totals = {"calls": 3, "cost_usd": 0.01}
    fake_models = [{"model": "claude-sonnet-4-5", "calls": 3, "cost_usd": 0.01}]
    monkeypatch.setattr(svc, "_cost_totals", lambda db, f: fake_totals)
    monkeypatch.setattr(svc, "_cost_group_by_model", lambda db, f: fake_models)

    result = await mod.get_cost_analytics(time_range="24h", db=MagicMock())

    assert result == {"totals": fake_totals, "by_model": fake_models}
    assert bifrost_calls == []
