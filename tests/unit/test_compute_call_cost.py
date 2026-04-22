"""Unit tests for ``daemon.agent_runner.compute_call_cost`` (GH #84 PR-E).

PR-E deleted the legacy Sonnet-pricing fallback so multi-provider calls
don't get silently misattributed. These tests lock in the new behavior:
resolve rates via ``model_registry`` when model+provider are present,
return 0.0 (and log) when either is missing or the registry fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


def _mock_registry(in_rate: float, out_rate: float):
    """Build a fake get_registry() that returns the given rates."""

    class _R:
        def get_cost_rates(self, model_id, provider_type):
            return (in_rate, out_rate)

    return _R()


def test_happy_path_uses_registry_rates():
    from daemon.agent_runner import compute_call_cost

    with patch(
        "services.model_registry.get_registry",
        return_value=_mock_registry(3.0 / 1_000_000, 15.0 / 1_000_000),
    ):
        cost = compute_call_cost("claude-sonnet-4-5-20250929", "anthropic", 1_000, 500)
    # 1000 * 3e-6 + 500 * 15e-6 = 0.003 + 0.0075 = 0.0105
    assert cost == pytest.approx(0.0105, rel=1e-9)


def test_openai_rates_applied_when_provider_is_openai():
    """Regression: pre-PR-E, a missing rate would silently bill at Sonnet.
    Now we look up the actual provider's rates."""
    from daemon.agent_runner import compute_call_cost

    with patch(
        "services.model_registry.get_registry",
        return_value=_mock_registry(2.5 / 1_000_000, 10.0 / 1_000_000),
    ):
        cost = compute_call_cost("gpt-4o", "openai", 1_000, 1_000)
    assert cost == pytest.approx((2.5 + 10.0) / 1_000, rel=1e-9)


def test_missing_model_returns_zero(caplog):
    """No fallback to Sonnet — missing metadata means we record $0 (visible
    on the dashboard) instead of a confidently-wrong number."""
    from daemon.agent_runner import compute_call_cost

    with caplog.at_level("WARNING"):
        cost = compute_call_cost(None, "anthropic", 1_000, 500)
    assert cost == 0.0
    assert any("missing model_id/provider_type" in r.message for r in caplog.records)


def test_missing_provider_returns_zero():
    from daemon.agent_runner import compute_call_cost

    assert compute_call_cost("claude-sonnet-4-5-20250929", None, 1_000, 500) == 0.0


def test_registry_exception_returns_zero(caplog):
    from daemon.agent_runner import compute_call_cost

    def _explode():
        raise RuntimeError("registry unavailable")

    with patch("services.model_registry.get_registry", side_effect=_explode), \
         caplog.at_level("WARNING"):
        cost = compute_call_cost("claude-sonnet-4-5", "anthropic", 1_000, 500)

    assert cost == 0.0
    # Warning surfaces the provider + model so an operator can diagnose
    # silently-zero costs from the /analytics/cost dashboard.
    assert any(
        "model_registry lookup failed" in r.message
        and "anthropic" in r.message
        for r in caplog.records
    )


def test_sonnet_constants_are_gone():
    """Guardrail against accidental reintroduction of the legacy fallback."""
    from daemon import agent_runner

    assert not hasattr(agent_runner, "SONNET_INPUT_COST")
    assert not hasattr(agent_runner, "SONNET_OUTPUT_COST")
