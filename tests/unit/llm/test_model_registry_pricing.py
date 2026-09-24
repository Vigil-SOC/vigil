"""Pricing from the gateway's datasheet, held in the registry's live meta.

Vigil keeps no rate card: every rate here is seeded the way the sync records a
``/api/models/details`` read, and anything the gateway does not price is
``unknown`` rather than free.
"""

from __future__ import annotations

import pytest

from core.llm.providers import registry as model_registry
from core.llm.providers.discovery import ModelMeta
from core.llm.providers.registry import (
    get_registry,
    infer_provider_type,
    record_live_meta,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_live_meta():
    model_registry.clear_live_meta()
    yield
    model_registry.clear_live_meta()


def _sheet(model_id, inp, out, read=None, write=None, **kw):
    return ModelMeta(
        id=model_id,
        display_name=model_id,
        input_cost_per_token=inp,
        output_cost_per_token=out,
        cache_read_cost_per_token=read,
        cache_write_cost_per_token=write,
        **kw,
    )


def test_datasheet_rates_are_exact_and_carry_their_fetch_time():
    record_live_meta(
        "anthropic",
        [_sheet("claude-opus-4-7", 5e-6, 2.5e-5, 5e-7, 6.25e-6)],
        rates_only=True,
    )
    rates = get_registry().get_rates("claude-opus-4-7", "anthropic")
    assert rates["pricing_source"] == "exact"
    assert (rates["input"], rates["output"]) == (5e-6, 2.5e-5)
    assert (rates["cache_read"], rates["cache_write"]) == (5e-7, 6.25e-6)
    assert rates["rates_fetched_at"]


def test_missing_cache_rates_charge_at_the_input_rate():
    record_live_meta("openai", [_sheet("gpt-x", 2e-6, 8e-6)], rates_only=True)
    assert get_registry().get_cache_rates("gpt-x", "openai") == (2e-6, 2e-6)
    source, rates = get_registry().get_call_pricing("gpt-x", "openai")
    assert source == "exact"
    assert rates == (2e-6, 8e-6, 2e-6, 2e-6)


def test_a_missing_input_or_output_rate_is_unknown_never_zero():
    record_live_meta("openai", [_sheet("half", 2e-6, None)], rates_only=True)
    assert get_registry().get_pricing_source("half", "openai") == "unknown"


def test_all_zero_rates_are_zero_not_exact():
    record_live_meta("vertex", [_sheet("free-tier", 0.0, 0.0)], rates_only=True)
    assert get_registry().get_pricing_source("free-tier", "vertex") == "zero"


def test_unlisted_model_is_unknown_and_counted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_registry, "_record_pricing_unknown", lambda p, m: calls.append((p, m))
    )
    assert (
        get_registry().get_pricing_source("claude-opus-4-7", "anthropic") == "unknown"
    )
    assert calls == [("anthropic", "claude-opus-4-7")]


def test_priced_model_does_not_increment_counter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_registry, "_record_pricing_unknown", lambda p, m: calls.append((p, m))
    )
    record_live_meta("anthropic", [_sheet("m", 1e-6, 2e-6)], rates_only=True)
    assert get_registry().get_pricing_source("m", "anthropic") == "exact"
    assert calls == []


def test_ollama_without_a_gateway_rate_is_self_hosted_zero():
    assert get_registry().get_pricing_source("llama3.1", "ollama") == "zero"


def test_ollama_override_on_the_gateway_is_charged():
    record_live_meta("ollama", [_sheet("llama3.1", 1e-7, 2e-7)], rates_only=True)
    assert get_registry().get_rates("llama3.1", "ollama")["pricing_source"] == "exact"


def test_discovery_and_datasheet_merge_in_either_order():
    discovered = ModelMeta(
        id="claude-x",
        display_name="Claude X",
        context_window=200_000,
        capabilities={"supports_tools": True, "supports_vision": True},
    )
    datasheet = _sheet("claude-x", 3e-6, 1.5e-5, context_window=1)

    record_live_meta("anthropic", [discovered])
    record_live_meta("anthropic", [datasheet], rates_only=True)
    record_live_meta("anthropic", [discovered])  # a later discovery run

    info = get_registry().get_model_info("p", "anthropic", "claude-x")
    assert info.display_name == "Claude X"
    assert info.context_window == 200_000
    assert info.supports_vision is True
    assert info.pricing_source == "exact"
    assert info.input_cost_per_1k == pytest.approx(3e-3)


def test_a_provider_config_change_keeps_the_gateway_rates():
    discovered = ModelMeta(id="m", display_name="M", context_window=10)
    record_live_meta("anthropic", [discovered])
    record_live_meta("anthropic", [_sheet("m", 1e-6, 2e-6)], rates_only=True)
    record_live_meta("anthropic", [ModelMeta(id="gone", display_name="Gone")])

    model_registry.invalidate_model_cache()

    info = get_registry().get_model_info("p", "anthropic", "m")
    assert info.pricing_source == "exact"
    assert info.context_window == 0
    assert ("anthropic", "gone") not in model_registry._LIVE_META


def test_infer_provider_type():
    assert infer_provider_type("vertex/gemini-flash") == "vertex"
    assert infer_provider_type("claude-sonnet-4-5-20250929") == "anthropic"
    assert infer_provider_type("gpt-4o-mini") == "openai"
    assert infer_provider_type("o3") == "openai"
    assert infer_provider_type("llama3.1") == "ollama"
    assert infer_provider_type("") == "unknown"
    assert infer_provider_type("some-novel-thing") == "unknown"
