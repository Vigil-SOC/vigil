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


def _mock_registry(
    in_rate: float,
    out_rate: float,
    cache_read_rate: float = 0.0,
    cache_creation_rate: float = 0.0,
):
    """Build a fake get_registry() that returns the given rates.

    Cache rates default to zero so existing tests that don't pass cache
    tokens behave identically to the pre-#184 cost math (input × in_rate
    + output × out_rate).
    """

    class _R:
        def get_cost_rates(self, model_id, provider_type):
            return (in_rate, out_rate)

        def get_cache_rates(self, model_id, provider_type):
            return (cache_read_rate, cache_creation_rate)

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

    with patch(
        "services.model_registry.get_registry", side_effect=_explode
    ), caplog.at_level("WARNING"):
        cost = compute_call_cost("claude-sonnet-4-5", "anthropic", 1_000, 500)

    assert cost == 0.0
    # Warning surfaces the provider + model so an operator can diagnose
    # silently-zero costs from the /analytics/cost dashboard.
    assert any(
        "model_registry lookup failed" in r.message and "anthropic" in r.message
        for r in caplog.records
    )


def test_sonnet_constants_are_gone():
    """Guardrail against accidental reintroduction of the legacy fallback."""
    from daemon import agent_runner

    assert not hasattr(agent_runner, "SONNET_INPUT_COST")
    assert not hasattr(agent_runner, "SONNET_OUTPUT_COST")


# ---------------------------------------------------------------------------
# #184 Phase 3 — cache-aware pricing
# ---------------------------------------------------------------------------


def test_cache_read_priced_at_anthropic_discount():
    """Cache reads bill at 0.1× input rate, not full input rate.

    Pre-#184 these tokens were ignored (priced at $0). After #184 we
    multiply by the Anthropic ephemeral-cache read multiplier.
    """
    from daemon.agent_runner import compute_call_cost

    in_rate = 3.0 / 1_000_000  # Sonnet input rate
    cache_read_rate = in_rate * 0.10
    with patch(
        "services.model_registry.get_registry",
        return_value=_mock_registry(
            in_rate, 15.0 / 1_000_000, cache_read_rate=cache_read_rate
        ),
    ):
        cost = compute_call_cost(
            "claude-sonnet-4-5-20250929",
            "anthropic",
            1_000,
            500,
            cache_read_tokens=10_000,
        )
    expected = 1_000 * in_rate + 500 * (15.0 / 1_000_000) + 10_000 * cache_read_rate
    assert cost == pytest.approx(expected, rel=1e-9)


def test_cache_creation_priced_at_anthropic_premium():
    """Cache writes bill at 1.25× input rate (the ephemeral premium)."""
    from daemon.agent_runner import compute_call_cost

    in_rate = 3.0 / 1_000_000
    cache_creation_rate = in_rate * 1.25
    with patch(
        "services.model_registry.get_registry",
        return_value=_mock_registry(
            in_rate,
            15.0 / 1_000_000,
            cache_creation_rate=cache_creation_rate,
        ),
    ):
        cost = compute_call_cost(
            "claude-sonnet-4-5-20250929",
            "anthropic",
            1_000,
            500,
            cache_creation_tokens=2_000,
        )
    expected = 1_000 * in_rate + 500 * (15.0 / 1_000_000) + 2_000 * cache_creation_rate
    assert cost == pytest.approx(expected, rel=1e-9)


def test_zero_cache_tokens_match_pre_184_behavior():
    """Backwards-compat: callers that don't pass cache tokens get the same
    number they did before #184."""
    from daemon.agent_runner import compute_call_cost

    in_rate = 3.0 / 1_000_000
    out_rate = 15.0 / 1_000_000
    with patch(
        "services.model_registry.get_registry",
        return_value=_mock_registry(in_rate, out_rate, cache_read_rate=in_rate * 0.1),
    ):
        legacy = compute_call_cost("claude-sonnet-4-5", "anthropic", 1_000, 500)
        explicit_zero = compute_call_cost(
            "claude-sonnet-4-5",
            "anthropic",
            1_000,
            500,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )
    assert legacy == explicit_zero
    assert legacy == pytest.approx(1_000 * in_rate + 500 * out_rate, rel=1e-9)


def test_real_anthropic_multipliers_via_registry():
    """End-to-end: hit the real ModelRegistry (no mock) and verify the
    Anthropic multipliers (0.1× read / 1.25× creation) are applied."""
    from daemon.agent_runner import compute_call_cost

    # Sonnet 4.5 has exact pricing in _CATALOG: $3/MTok in, $15/MTok out.
    cost = compute_call_cost(
        "claude-sonnet-4-5-20250929",
        "anthropic",
        1_000,
        500,
        cache_read_tokens=10_000,
        cache_creation_tokens=2_000,
    )
    in_rate = 3.0 / 1_000_000
    out_rate = 15.0 / 1_000_000
    expected = (
        1_000 * in_rate
        + 500 * out_rate
        + 10_000 * in_rate * 0.10
        + 2_000 * in_rate * 1.25
    )
    assert cost == pytest.approx(expected, rel=1e-9)
