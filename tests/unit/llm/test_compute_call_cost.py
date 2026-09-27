"""Unit tests for ``core.llm.cost.calls.compute_call_cost`` (GH #84 PR-E).

PR-E deleted the legacy Sonnet-pricing fallback so multi-provider calls
don't get silently misattributed. These tests lock in the new behavior:
resolve rates via ``model_registry`` when model+provider are present,
return None (unpriced, #1115) when either is missing, the registry fails, or
the model's pricing source is ``unknown``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


def _mock_registry(
    in_rate: float,
    out_rate: float,
    cache_read_rate: float = 0.0,
    cache_creation_rate: float = 0.0,
    source: str = "exact",
):
    """Build a fake get_registry() that returns the given rates.

    Cache rates default to zero so existing tests that don't pass cache
    tokens behave identically to the pre-#184 cost math (input × in_rate
    + output × out_rate).
    """

    class _R:
        def get_rates(self, model_id, provider_type):
            return {
                "input": in_rate,
                "output": out_rate,
                "cache_read": cache_read_rate,
                "cache_write": cache_creation_rate,
                "pricing_source": source,
                "rates_fetched_at": None
                if source == "unknown"
                else "2026-01-01T00:00:00+00:00",
            }

    return _R()


def test_happy_path_uses_registry_rates():
    from core.llm.cost.calls import compute_call_cost

    with patch(
        "core.llm.providers.registry.get_registry",
        return_value=_mock_registry(3.0 / 1_000_000, 15.0 / 1_000_000),
    ):
        cost = compute_call_cost("claude-sonnet-4-5-20250929", "anthropic", 1_000, 500)
    # 1000 * 3e-6 + 500 * 15e-6 = 0.003 + 0.0075 = 0.0105
    assert cost == pytest.approx(0.0105, rel=1e-9)


def test_openai_rates_applied_when_provider_is_openai():
    """Regression: pre-PR-E, a missing rate would silently bill at Sonnet.
    Now we look up the actual provider's rates."""
    from core.llm.cost.calls import compute_call_cost

    with patch(
        "core.llm.providers.registry.get_registry",
        return_value=_mock_registry(2.5 / 1_000_000, 10.0 / 1_000_000),
    ):
        cost = compute_call_cost("gpt-4o", "openai", 1_000, 1_000)
    assert cost == pytest.approx((2.5 + 10.0) / 1_000, rel=1e-9)


def test_missing_model_is_unpriced(caplog):
    """No fallback to Sonnet — missing metadata is recorded as unpriced
    rather than a confidently-wrong number or a false $0."""
    from core.llm.cost.calls import compute_call_cost

    with caplog.at_level("WARNING"):
        cost = compute_call_cost(None, "anthropic", 1_000, 500)
    assert cost is None
    assert any("missing model_id/provider_type" in r.message for r in caplog.records)


def test_missing_provider_is_unpriced():
    from core.llm.cost.calls import compute_call_cost

    assert compute_call_cost("claude-sonnet-4-5-20250929", None, 1_000, 500) is None


def test_registry_exception_is_unpriced(caplog):
    from core.llm.cost.calls import compute_call_cost

    def _explode():
        raise RuntimeError("registry unavailable")

    with patch(
        "core.llm.providers.registry.get_registry", side_effect=_explode
    ), caplog.at_level("WARNING"):
        cost = compute_call_cost("claude-sonnet-4-5", "anthropic", 1_000, 500)

    assert cost is None
    # Warning surfaces the provider + model so an operator can diagnose
    # unpriced calls on the /analytics/cost dashboard.
    assert any(
        "model_registry lookup failed" in r.message and "anthropic" in r.message
        for r in caplog.records
    )


def test_unknown_pricing_source_is_unpriced():
    from core.llm.cost.calls import compute_call_cost

    with patch(
        "core.llm.providers.registry.get_registry",
        return_value=_mock_registry(0.0, 0.0, source="unknown"),
    ):
        assert compute_call_cost("mystery-model", "openai", 1_000, 500) is None


def test_zero_pricing_source_is_a_real_zero():
    from core.llm.cost.calls import compute_call_cost

    with patch(
        "core.llm.providers.registry.get_registry",
        return_value=_mock_registry(0.0, 0.0, source="zero"),
    ):
        assert compute_call_cost("llama3", "ollama", 1_000, 500) == 0.0


def test_real_registry_unknown_and_zero_models():
    """Against the real catalog: an uncatalogued model is unpriced, and one
    catalog read fires the pricing-unknown counter exactly once."""
    from core.llm.cost.calls import compute_call_cost

    with patch("core.llm.providers.registry._record_pricing_unknown") as counter:
        assert compute_call_cost("no-such-model-xyz", "openai", 1_000, 500) is None
    counter.assert_called_once_with("openai", "no-such-model-xyz")
    assert compute_call_cost("llama3", "ollama", 1_000, 500) == 0.0


def test_sonnet_constants_are_gone():
    """Guardrail against accidental reintroduction of the legacy fallback.

    The module that held them is gone with #629, which is the stronger form of
    the same claim: there is nowhere for the constants to come back to.
    """
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.daemon.agent_runner")


# ---------------------------------------------------------------------------
# #184 Phase 3 — cache-aware pricing
# ---------------------------------------------------------------------------


def test_cache_read_priced_at_anthropic_discount():
    """Cache reads bill at 0.1× input rate, not full input rate.

    Pre-#184 these tokens were ignored (priced at $0). After #184 we
    multiply by the Anthropic ephemeral-cache read multiplier.
    """
    from core.llm.cost.calls import compute_call_cost

    in_rate = 3.0 / 1_000_000  # Sonnet input rate
    cache_read_rate = in_rate * 0.10
    with patch(
        "core.llm.providers.registry.get_registry",
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
    from core.llm.cost.calls import compute_call_cost

    in_rate = 3.0 / 1_000_000
    cache_creation_rate = in_rate * 1.25
    with patch(
        "core.llm.providers.registry.get_registry",
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
    from core.llm.cost.calls import compute_call_cost

    in_rate = 3.0 / 1_000_000
    out_rate = 15.0 / 1_000_000
    with patch(
        "core.llm.providers.registry.get_registry",
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


def test_real_registry_prices_from_the_datasheet_cache_rates():
    """End-to-end: hit the real ModelRegistry (no mock) seeded as the sync
    records a datasheet read, and verify its per-model cache rates apply."""
    from core.llm.cost.calls import compute_call_cost
    from core.llm.providers import registry as model_registry
    from core.llm.providers.discovery import ModelMeta

    in_rate = 3.0 / 1_000_000
    out_rate = 15.0 / 1_000_000
    model_registry.record_live_meta(
        "anthropic",
        [
            ModelMeta(
                "claude-sonnet-4-5-20250929",
                "claude-sonnet-4-5-20250929",
                input_cost_per_token=in_rate,
                output_cost_per_token=out_rate,
                cache_read_cost_per_token=in_rate * 0.10,
                cache_write_cost_per_token=in_rate * 1.25,
            )
        ],
        rates_only=True,
    )
    try:
        cost = compute_call_cost(
            "claude-sonnet-4-5-20250929",
            "anthropic",
            1_000,
            500,
            cache_read_tokens=10_000,
            cache_creation_tokens=2_000,
        )
    finally:
        model_registry.clear_live_meta()
    expected = (
        1_000 * in_rate
        + 500 * out_rate
        + 10_000 * in_rate * 0.10
        + 2_000 * in_rate * 1.25
    )
    assert cost == pytest.approx(expected, rel=1e-9)
