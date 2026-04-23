"""Unit tests for services.model_registry (GH #89).

Focus: pure logic (cost catalog, capability lookup) and fallback resolution.
DB-backed paths are exercised via monkeypatching the DB accessors so the
tests don't require a live Postgres.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.model_registry import COMPONENTS  # noqa: E402
from services.model_registry import (  # noqa: E402
    ComponentAssignment,
    ModelRegistry,
    _catalog_entry,
    is_valid_component,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Catalog / cost lookups (pure)
# ---------------------------------------------------------------------------


def test_components_enum_includes_all_seven():
    expected = {
        "chat_default",
        "triage",
        "investigation",
        "orchestrator_plan",
        "orchestrator_review",
        "summarization",
        "reporting",
    }
    assert set(COMPONENTS) == expected


def test_is_valid_component():
    assert is_valid_component("chat_default") is True
    assert is_valid_component("nope") is False


def test_cost_rates_known_anthropic_model():
    input_rate, output_rate = ModelRegistry.get_cost_rates(
        "claude-sonnet-4-5-20250929", "anthropic"
    )
    # Catalog says $3/$15 per 1M tokens.
    assert input_rate == pytest.approx(3.0 / 1_000_000)
    assert output_rate == pytest.approx(15.0 / 1_000_000)


def test_cost_rates_ollama_is_zero():
    # Ollama is self-hosted → zero cost by design.
    input_rate, output_rate = ModelRegistry.get_cost_rates("llama3.1:8b", "ollama")
    assert input_rate == 0.0
    assert output_rate == 0.0


def test_cost_rates_unknown_cloud_model_degrades_gracefully():
    # Unknown models return (0, 0) and log a warning — we're asserting the
    # fallback behavior is safe (no exception).
    input_rate, output_rate = ModelRegistry.get_cost_rates(
        "does-not-exist-1.0", "openai"
    )
    assert input_rate == 0.0
    assert output_rate == 0.0


def test_get_model_info_populates_capabilities():
    info = ModelRegistry.get_model_info(
        provider_id="anthropic-default",
        provider_type="anthropic",
        model_id="claude-sonnet-4-5-20250929",
    )
    assert info.model_id == "claude-sonnet-4-5-20250929"
    assert info.provider_id == "anthropic-default"
    assert info.supports_tools is True
    assert info.supports_thinking is True
    assert info.context_window == 200_000


def test_catalog_entry_ollama_has_no_tools():
    entry = _catalog_entry("ollama", "llama3.1:8b")
    assert entry["supports_tools"] is False
    assert entry["input_per_m"] == 0.0


# ---------------------------------------------------------------------------
# Layered catalog — tier heuristic + live meta + pricing_source (GH #139)
# ---------------------------------------------------------------------------


def test_tier_heuristic_anthropic_haiku_future():
    """A model NOT in the exact catalog should fall to the heuristic
    layer and get tier pricing, not $0. Uses a hypothetical future
    Haiku variant so the test survives _CATALOG additions."""
    entry = _catalog_entry("anthropic", "claude-haiku-9-9-hypothetical")
    assert entry["input_per_m"] == pytest.approx(0.80)
    assert entry["output_per_m"] == pytest.approx(4.0)
    assert entry["pricing_source"] == "heuristic"


def test_tier_heuristic_openai_gpt4o_mini():
    entry = _catalog_entry("openai", "gpt-4o-mini-2024-07-18")
    assert entry["input_per_m"] == pytest.approx(0.15)
    assert entry["output_per_m"] == pytest.approx(0.60)
    assert entry["pricing_source"] == "heuristic"


def test_tier_heuristic_openai_o3_mini_orders_before_o1():
    """Regex order matters: o3-mini should NOT match o3 first."""
    entry = _catalog_entry("openai", "o3-mini")
    assert entry["input_per_m"] == pytest.approx(1.10)
    assert entry["pricing_source"] == "heuristic"


def test_exact_catalog_wins_over_heuristic():
    # claude-sonnet-4-5 is in _CATALOG → "exact", even though it also
    # matches the sonnet tier.
    entry = _catalog_entry("anthropic", "claude-sonnet-4-5-20250929")
    assert entry["pricing_source"] == "exact"
    assert entry["input_per_m"] == pytest.approx(3.0)


def test_pricing_source_unknown_for_unrecognized_model():
    entry = _catalog_entry("openai", "completely-unknown-xyz")
    assert entry["pricing_source"] == "unknown"
    assert entry["input_per_m"] == 0.0


def test_live_meta_populates_context_window(monkeypatch):
    """record_live_meta should feed display_name / context / caps into
    the catalog lookup for models not in the static _CATALOG."""
    from services import model_registry

    class _M:
        id = "claude-haiku-3-5-20241022"
        display_name = "Claude Haiku 3.5 (live)"
        context_window = 200_000
        capabilities = {
            "supports_tools": True,
            "supports_thinking": False,
            "supports_vision": True,
        }

    try:
        model_registry.record_live_meta("anthropic", [_M()])
        entry = _catalog_entry("anthropic", "claude-haiku-3-5-20241022")
        assert entry["context_window"] == 200_000
        assert entry["display_name"] == "Claude Haiku 3.5 (live)"
        assert entry["supports_vision"] is True
        # Pricing still comes from the tier heuristic for this id.
        assert entry["pricing_source"] == "heuristic"
    finally:
        model_registry.clear_live_meta("anthropic")


def test_get_model_info_deprecated_flag():
    info = ModelRegistry.get_model_info(
        provider_id="anthropic-default",
        provider_type="anthropic",
        model_id="claude-sonnet-4-5-20250929",
        deprecated=True,
    )
    assert info.deprecated is True
    d = info.to_dict()
    assert d["deprecated"] is True
    assert d["pricing_source"] == "exact"


# ---------------------------------------------------------------------------
# Extras mechanism — force-include IDs upstream dropped from /v1/models
# ---------------------------------------------------------------------------


def test_default_extras_include_anthropic_3x(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_EXTRA_MODELS", raising=False)
    from services.model_registry import get_extra_model_ids

    ids = get_extra_model_ids("anthropic")
    assert "claude-3-5-haiku-20241022" in ids
    assert "claude-3-5-sonnet-20241022" in ids
    assert "claude-3-haiku-20240307" in ids


def test_env_override_replaces_defaults(monkeypatch):
    from services.model_registry import get_extra_model_ids

    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "foo-1, bar-2 ,, baz-3")
    ids = get_extra_model_ids("anthropic")
    assert ids == ("foo-1", "bar-2", "baz-3")


def test_env_empty_string_disables_extras(monkeypatch):
    from services.model_registry import get_extra_model_ids

    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")
    assert get_extra_model_ids("anthropic") == ()


def test_extras_catalog_entry_has_exact_pricing():
    """3.x entries were added to _CATALOG so they render with correct
    context/pricing instead of the tier heuristic fallback."""
    entry = _catalog_entry("anthropic", "claude-3-5-haiku-20241022")
    assert entry["pricing_source"] == "exact"
    assert entry["context_window"] == 200_000
    assert entry["input_per_m"] == pytest.approx(0.80)

    entry = _catalog_entry("anthropic", "claude-3-haiku-20240307")
    assert entry["input_per_m"] == pytest.approx(0.25)
    assert entry["output_per_m"] == pytest.approx(1.25)


def test_is_extra_model_flips_after_registration():
    from services import model_registry

    provider_type = "anthropic"
    mid = "test-extra-registration-xyz"
    assert model_registry.is_extra_model(provider_type, mid) is False
    model_registry._register_extras(provider_type, (mid,))
    try:
        assert model_registry.is_extra_model(provider_type, mid) is True
    finally:
        model_registry._EXTRA_IDS.discard((provider_type, mid))


# ---------------------------------------------------------------------------
# Fallback resolution — DB mocked via registry internals
# ---------------------------------------------------------------------------


class _StubRegistry(ModelRegistry):
    """ModelRegistry that returns canned DB responses without touching a DB."""

    def __init__(
        self,
        *,
        assignments: Optional[Dict[str, ComponentAssignment]] = None,
        default_anthropic: Optional[Dict[str, str]] = None,
    ):
        super().__init__()
        self._assignments = assignments or {}
        self._default_anthropic = default_anthropic

    def get_all_assignments(  # type: ignore[override]
        self,
    ) -> Dict[str, ComponentAssignment]:
        return self._assignments

    def _default_anthropic_provider(self):  # type: ignore[override]
        return self._default_anthropic


def test_resolve_uses_explicit_component_assignment():
    reg = _StubRegistry(
        assignments={
            "triage": ComponentAssignment(
                component="triage",
                provider_id="ollama-local",
                model_id="llama3:latest",
            ),
            "chat_default": ComponentAssignment(
                component="chat_default",
                provider_id="anthropic-default",
                model_id="claude-sonnet-4-5-20250929",
            ),
        },
    )
    provider, model = reg.resolve_model_for_component("triage")
    assert provider == "ollama-local"
    assert model == "llama3:latest"


def test_resolve_falls_back_to_chat_default():
    reg = _StubRegistry(
        assignments={
            "chat_default": ComponentAssignment(
                component="chat_default",
                provider_id="anthropic-default",
                model_id="claude-sonnet-4-5-20250929",
            ),
        },
    )
    # summarization has no explicit row → chat_default wins.
    provider, model = reg.resolve_model_for_component("summarization")
    assert provider == "anthropic-default"
    assert model == "claude-sonnet-4-5-20250929"


def test_resolve_falls_back_to_default_anthropic_when_db_empty():
    reg = _StubRegistry(
        assignments={},
        default_anthropic={
            "provider_id": "anthropic-default",
            "default_model": "claude-sonnet-4-5-20250929",
        },
    )
    provider, model = reg.resolve_model_for_component("investigation")
    assert provider == "anthropic-default"
    assert model == "claude-sonnet-4-5-20250929"


def test_resolve_returns_none_when_no_db_and_no_anthropic():
    reg = _StubRegistry(assignments={}, default_anthropic=None)
    assert reg.resolve_model_for_component("chat_default") is None


def test_agent_override_pins_model_but_uses_default_provider():
    reg = _StubRegistry(
        assignments={},
        default_anthropic={
            "provider_id": "anthropic-default",
            "default_model": "claude-sonnet-4-5-20250929",
        },
    )
    provider, model = reg.resolve_model_for_component(
        "triage", agent_override="claude-opus-4-20250514"
    )
    assert provider == "anthropic-default"
    assert model == "claude-opus-4-20250514"
