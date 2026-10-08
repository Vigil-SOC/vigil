"""Who serves a model is read from configuration, never guessed from its name (#986).

The agent layer calls one gateway; the catalog is keyed by whoever actually
answered. Before this, a bare id with no priced provider fell through to a
substring match, so ``llama-3.3-70b-versatile`` on a commercial host priced at
$0 with source ``zero`` -- a figure the ledger records as genuinely free.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.llm.router.router as router_mod
from core.llm.cost.pricing_router import priced_as
from core.llm.providers.registry import get_registry

pytestmark = pytest.mark.unit


def _default(provider_type):
    return lambda: SimpleNamespace(provider_type=provider_type)


class TestACommerciallyHostedOpenWeightModel:
    def test_is_priced_by_the_configured_provider_not_its_name(self, monkeypatch):
        monkeypatch.setattr(router_mod, "get_default_provider_spec", _default("groq"))

        provider, bare = priced_as("bifrost", "llama-3.3-70b-versatile")

        assert provider == "groq"
        assert bare == "llama-3.3-70b-versatile"
        # No rate card for the host, so unpriced -- which the agent refuses to
        # spend against. Never the free-looking "zero".
        assert get_registry().get_pricing_source(bare, provider) == "unknown"

    def test_the_caller_named_host_is_final_even_without_a_rate_card(self, monkeypatch):
        monkeypatch.setattr(router_mod, "get_default_provider_spec", _default("ollama"))

        provider, _ = priced_as("groq", "llama-3.3-70b-versatile")

        assert provider == "groq"

    def test_is_never_labelled_zero_when_no_record_resolves(self, monkeypatch):
        monkeypatch.setattr(router_mod, "get_default_provider_spec", lambda: None)

        provider, bare = priced_as("bifrost", "mixtral-8x7b-32768")

        assert provider == "unknown"
        assert get_registry().get_pricing_source(bare, provider) == "unknown"

    def test_a_failing_lookup_prices_nothing_rather_than_guessing(self, monkeypatch):
        def boom():
            raise RuntimeError("no database")

        monkeypatch.setattr(router_mod, "get_default_provider_spec", boom)

        assert priced_as("bifrost", "qwen2.5-72b-instruct")[0] == "unknown"


class TestASelfHostedEndpointServingACommerciallyNamedModel:
    def test_the_caller_declared_provider_wins_over_the_name(self):
        provider, bare = priced_as("ollama", "claude-sonnet-4-5")

        assert (provider, bare) == ("ollama", "claude-sonnet-4-5")
        assert get_registry().get_pricing_source(bare, provider) == "zero"
        assert get_registry().get_cost_rates(bare, provider) == (0.0, 0.0)

    def test_the_configured_default_wins_over_the_name(self, monkeypatch):
        monkeypatch.setattr(router_mod, "get_default_provider_spec", _default("ollama"))

        provider, _ = priced_as("bifrost", "gpt-4o")

        assert provider == "ollama"


class TestANamespacedIdStillNamesItsProvider:
    def test_the_prefix_is_read_before_any_record(self, monkeypatch):
        monkeypatch.setattr(router_mod, "get_default_provider_spec", _default("ollama"))

        assert priced_as("bifrost", "anthropic/claude-sonnet-4-5") == (
            "anthropic",
            "claude-sonnet-4-5",
        )


class TestAClaudeIdServedThroughOpenRouter:
    """``anthropic/claude-haiku-5.5`` on an openrouter provider stays openrouter's
    and is priced from OpenRouter's sheet, else the vendor's (#1931)."""

    def _record(self, provider_type, model_id, in_rate=1e-6, out_rate=5e-6):
        from core.llm.providers.discovery import ModelMeta
        from core.llm.providers.registry import clear_live_meta, record_live_meta

        clear_live_meta()
        record_live_meta(
            provider_type,
            [
                ModelMeta(
                    id=model_id,
                    display_name=model_id,
                    input_cost_per_token=in_rate,
                    output_cost_per_token=out_rate,
                )
            ],
            rates_only=True,
        )

    def test_the_vendor_prefix_is_not_a_provider_to_price_as(self):
        assert priced_as("openrouter", "anthropic/claude-haiku-5.5") == (
            "openrouter",
            "anthropic/claude-haiku-5.5",
        )
        assert priced_as("bifrost", "openrouter/anthropic/claude-haiku-5.5") == (
            "openrouter",
            "anthropic/claude-haiku-5.5",
        )

    def test_openrouters_own_sheet_wins(self):
        self._record("openrouter", "anthropic/claude-haiku-5.5", 2e-6, 9e-6)

        rates = get_registry().get_rates("anthropic/claude-haiku-5.5", "openrouter")

        assert (rates["input"], rates["pricing_source"]) == (2e-6, "exact")

    def test_falls_back_to_the_vendors_sheet_with_dots_as_dashes(self):
        self._record("anthropic", "claude-haiku-5-5")

        rates = get_registry().get_rates("anthropic/claude-haiku-5.5", "openrouter")

        assert (rates["input"], rates["output"]) == (1e-6, 5e-6)
        assert rates["pricing_source"] == "exact"

    def test_an_id_no_sheet_prices_stays_unknown(self):
        self._record("anthropic", "claude-haiku-5-5")

        rates = get_registry().get_rates("anthropic/claude-nope-9.9", "openrouter")

        assert rates["pricing_source"] == "unknown"
