"""Tests for the model_registry pricing helpers added in #184.

Covers:
  - get_cache_multipliers (provider-level lookup)
  - get_cache_rates (composed: input rate × multiplier)
  - get_pricing_source (visibility into which catalog layer answered)
  - infer_provider_type (used by analytics to badge rows)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


def test_anthropic_cache_multipliers():
    from services.model_registry import get_cache_multipliers

    read_mult, creation_mult = get_cache_multipliers("anthropic")
    # Anthropic 5-min ephemeral cache: 0.1× read, 1.25× creation.
    assert read_mult == pytest.approx(0.10)
    assert creation_mult == pytest.approx(1.25)


def test_openai_cache_multipliers():
    from services.model_registry import get_cache_multipliers

    read_mult, creation_mult = get_cache_multipliers("openai")
    # OpenAI: 0.5× cached read, no write premium.
    assert read_mult == pytest.approx(0.50)
    assert creation_mult == pytest.approx(0.0)


def test_unknown_provider_falls_back_to_full_input_rate():
    """Unknown providers should over-bound, not under-bound — charge cache
    tokens at full input rate so we don't silently miss costs."""
    from services.model_registry import get_cache_multipliers

    assert get_cache_multipliers("future-vendor") == (1.0, 1.0)


def test_get_cache_rates_uses_input_rate_times_multiplier():
    """The cache rate is derived from the input rate, so a repricing of
    input automatically reprices cache — no second table to maintain."""
    from services.model_registry import get_registry

    registry = get_registry()
    in_rate, _ = registry.get_cost_rates("claude-sonnet-4-5-20250929", "anthropic")
    cache_read, cache_creation = registry.get_cache_rates(
        "claude-sonnet-4-5-20250929", "anthropic"
    )
    assert cache_read == pytest.approx(in_rate * 0.10)
    assert cache_creation == pytest.approx(in_rate * 1.25)


def test_pricing_source_exact_for_catalog_models():
    from services.model_registry import get_registry

    src = get_registry().get_pricing_source("claude-sonnet-4-5-20250929", "anthropic")
    assert src == "exact"


def test_pricing_source_heuristic_for_unknown_anthropic_variant():
    """A model id we haven't catalog'd but matches a tier regex (e.g.
    a future Sonnet variant) should resolve via heuristic."""
    from services.model_registry import get_registry

    src = get_registry().get_pricing_source("claude-sonnet-9-99-future", "anthropic")
    assert src == "heuristic"


def test_pricing_source_zero_for_ollama():
    from services.model_registry import get_registry

    assert get_registry().get_pricing_source("llama3.1", "ollama") == "zero"


def test_pricing_source_unknown_for_novel_provider():
    from services.model_registry import get_registry

    assert get_registry().get_pricing_source("foo-1", "future-vendor") == "unknown"


def test_unknown_pricing_increments_counter(monkeypatch):
    """#184 acceptance #2: the 'unknown' path must increment the OTEL
    counter so dashboards/alerts can see unsupported models, not silently
    record $0."""
    from services import model_registry

    calls = []
    monkeypatch.setattr(
        model_registry,
        "_record_pricing_unknown",
        lambda provider_type, model_id: calls.append((provider_type, model_id)),
    )
    # Trigger the unknown branch via the public API.
    src = model_registry.get_registry().get_pricing_source("foo-1", "future-vendor")
    assert src == "unknown"
    assert ("future-vendor", "foo-1") in calls


def test_known_pricing_does_not_increment_counter(monkeypatch):
    """Known models must NOT increment the unknown-pricing counter."""
    from services import model_registry

    calls = []
    monkeypatch.setattr(
        model_registry,
        "_record_pricing_unknown",
        lambda provider_type, model_id: calls.append((provider_type, model_id)),
    )
    # claude-sonnet-4-5-20250929 should resolve to "exact" via _CATALOG.
    src = model_registry.get_registry().get_pricing_source(
        "claude-sonnet-4-5-20250929", "anthropic"
    )
    assert src in ("exact", "heuristic")
    assert calls == []


def test_infer_provider_type_anthropic():
    from services.model_registry import infer_provider_type

    assert infer_provider_type("claude-sonnet-4-5-20250929") == "anthropic"
    assert infer_provider_type("claude-opus-4-7") == "anthropic"


def test_infer_provider_type_openai():
    from services.model_registry import infer_provider_type

    assert infer_provider_type("gpt-4o") == "openai"
    assert infer_provider_type("gpt-4o-mini") == "openai"
    assert infer_provider_type("o1-mini") == "openai"
    assert infer_provider_type("o3") == "openai"


def test_infer_provider_type_ollama_soft_match():
    from services.model_registry import infer_provider_type

    assert infer_provider_type("llama3.1") == "ollama"
    assert infer_provider_type("mistral-7b") == "ollama"
    assert infer_provider_type("qwen2.5") == "ollama"


def test_infer_provider_type_unknown():
    from services.model_registry import infer_provider_type

    assert infer_provider_type("") == "unknown"
    assert infer_provider_type("some-novel-thing") == "unknown"
