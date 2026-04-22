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

from services.model_registry import (  # noqa: E402
    COMPONENTS,
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
