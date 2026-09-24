"""Unit tests for core.llm.providers.registry (GH #89).

Focus: pure logic (cost catalog, capability lookup) and fallback resolution.
DB-backed paths are exercised via monkeypatching the DB accessors so the
tests don't require a live Postgres.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.llm.providers import registry as model_registry  # noqa: E402
from core.llm.providers.discovery import ModelMeta  # noqa: E402
from core.llm.providers.registry import COMPONENTS  # noqa: E402
from core.llm.providers.registry import (  # noqa: E402
    ComponentAssignment,
    ModelRegistry,
    _catalog_entry,
    is_valid_component,
)
from core.llm.router.router import ProviderSpec  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Catalog / cost lookups (pure)
# ---------------------------------------------------------------------------


def test_components_enum():
    expected = {
        "chat_default",
        "triage",
        "investigation",
        "summarization",
        "reporting",
    }
    assert set(COMPONENTS) == expected


def test_is_valid_component():
    assert is_valid_component("chat_default") is True
    assert is_valid_component("nope") is False


def _seed(provider_type, model_id, inp=3e-6, out=1.5e-5, **caps):
    """Record one model the way the sync records discovery plus the datasheet."""
    model_registry.record_live_meta(
        provider_type,
        [
            ModelMeta(
                id=model_id,
                display_name=caps.pop("display_name", model_id),
                context_window=caps.pop("context_window", 0),
                capabilities=caps,
                input_cost_per_token=inp,
                output_cost_per_token=out,
            )
        ],
    )


@pytest.fixture(autouse=True)
def _clean_live_meta():
    model_registry.clear_live_meta()
    yield
    model_registry.clear_live_meta()


def test_cost_rates_come_from_live_meta():
    _seed("anthropic", "claude-sonnet-4-5-20250929")
    input_rate, output_rate = ModelRegistry.get_cost_rates(
        "claude-sonnet-4-5-20250929", "anthropic"
    )
    assert input_rate == pytest.approx(3e-6)
    assert output_rate == pytest.approx(1.5e-5)


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
    assert ModelRegistry.get_pricing_source("does-not-exist-1.0", "openai") == "unknown"


def test_get_model_info_populates_capabilities():
    _seed(
        "anthropic",
        "claude-sonnet-4-5-20250929",
        context_window=200_000,
        supports_tools=True,
        supports_thinking=True,
    )
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
    assert info.pricing_source == "exact"


def test_catalog_entry_ollama_has_no_tools():
    entry = _catalog_entry("ollama", "llama3.1:8b")
    assert entry["supports_tools"] is False
    assert entry["input"] == 0.0


def test_a_model_without_gateway_rates_is_unknown_whatever_its_name():
    # No tier guess from the id: an unpriced opus is unpriced, not $15/$75.
    entry = _catalog_entry("anthropic", "claude-opus-9-hypothetical")
    assert entry["pricing_source"] == "unknown"
    assert entry["input"] == 0.0


def test_live_meta_without_rates_keeps_caps_and_stays_unknown():
    class _M:
        id = "claude-haiku-3-5-20241022"
        display_name = "Claude Haiku 3.5 (live)"
        context_window = 200_000
        capabilities = {"supports_tools": True, "supports_vision": True}

    model_registry.record_live_meta("anthropic", [_M()])
    entry = _catalog_entry("anthropic", "claude-haiku-3-5-20241022")
    assert entry["context_window"] == 200_000
    assert entry["display_name"] == "Claude Haiku 3.5 (live)"
    assert entry["supports_vision"] is True
    assert entry["pricing_source"] == "unknown"


def test_get_model_info_deprecated_flag():
    _seed("anthropic", "claude-sonnet-4-5-20250929")
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
    from core.llm.providers.registry import get_extra_model_ids

    ids = get_extra_model_ids("anthropic")
    assert "claude-3-5-haiku-20241022" in ids
    assert "claude-3-5-sonnet-20241022" in ids
    assert "claude-3-haiku-20240307" in ids


def test_env_override_replaces_defaults(monkeypatch):
    from core.llm.providers.registry import get_extra_model_ids

    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "foo-1, bar-2 ,, baz-3")
    ids = get_extra_model_ids("anthropic")
    assert ids == ("foo-1", "bar-2", "baz-3")


def test_env_empty_string_disables_extras(monkeypatch):
    from core.llm.providers.registry import get_extra_model_ids

    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "")
    assert get_extra_model_ids("anthropic") == ()


def test_is_extra_model_flips_after_registration():
    from core.llm.providers import registry as model_registry

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


class _Prov:
    """Minimal stand-in for an LLMProviderConfig row."""

    def __init__(self, provider_id, provider_type, default_model=None):
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.default_model = default_model


class _StubRegistry(ModelRegistry):
    """ModelRegistry that returns canned DB responses without touching a DB."""

    def __init__(
        self,
        *,
        assignments: Optional[Dict[str, ComponentAssignment]] = None,
        default_anthropic: Optional[Dict[str, str]] = None,
        active_providers=None,
    ):
        super().__init__()
        self._assignments = assignments or {}
        self._default_anthropic = default_anthropic
        self._active = active_providers or []

    def get_all_assignments(  # type: ignore[override]
        self,
    ) -> Dict[str, ComponentAssignment]:
        return self._assignments

    def _default_anthropic_provider(self):  # type: ignore[override]
        return self._default_anthropic

    def _active_providers(self):  # type: ignore[override]
        return self._active


OLLAMA_DEFAULT = ProviderSpec(
    provider_id="bifrost-ollama",
    provider_type="ollama",
    base_url=None,
    api_key_ref=None,
    default_model="llama3.1:8b",
    config={},
)


@pytest.fixture
def default_provider(monkeypatch):
    """Set what the terminal rung (get_default_provider_spec) returns."""

    def _set(spec: Optional[ProviderSpec]) -> None:
        monkeypatch.setattr(model_registry, "get_default_provider_spec", lambda: spec)

    _set(None)
    return _set


def test_resolve_uses_explicit_component_assignment(default_provider):
    default_provider(OLLAMA_DEFAULT)
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


def test_resolve_falls_back_to_chat_default(default_provider):
    # An existing chat_default row (e.g. an upgraded install) outranks the rung.
    default_provider(OLLAMA_DEFAULT)
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


def test_resolve_without_assignments_uses_default_provider_of_any_type(
    default_provider,
):
    # An Anthropic default row must not outrank the provider-agnostic rung (#1005).
    default_provider(OLLAMA_DEFAULT)
    reg = _StubRegistry(
        assignments={},
        default_anthropic={
            "provider_id": "bifrost-anthropic",
            "default_model": "claude-sonnet-4-6",
        },
    )
    assert reg.resolve_model_for_component("investigation") == (
        "bifrost-ollama",
        "llama3.1:8b",
    )


def test_resolve_returns_none_when_no_provider_is_active(default_provider):
    reg = _StubRegistry(assignments={})
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


# ---------------------------------------------------------------------------
# Non-Anthropic models in the picker (GH #409)
# ---------------------------------------------------------------------------


async def test_list_available_models_floors_empty_provider_to_default(monkeypatch):
    """An active Ollama provider whose discovery returns nothing (empty
    bootstrap) must still surface its configured default_model, not vanish
    and let the aggregate collapse to Anthropic (#409)."""
    from core.llm.providers import registry as model_registry

    async def _no_models(_row):
        return []

    monkeypatch.setattr(model_registry, "fetch_provider_models", _no_models)
    reg = _StubRegistry(
        active_providers=[_Prov("ollama-local", "ollama", default_model="llama3.1:8b")]
    )
    models = await reg.list_available_models()
    assert [m.model_id for m in models] == ["llama3.1:8b"]
    assert all(m.provider_type == "ollama" for m in models)


async def test_list_available_models_uses_live_ids_when_present(monkeypatch):
    """When discovery returns real ids the default_model floor is not used."""
    from core.llm.providers import registry as model_registry

    async def _live(_row):
        return ["llama3.1:8b", "mistral:7b"]

    monkeypatch.setattr(model_registry, "fetch_provider_models", _live)
    reg = _StubRegistry(
        active_providers=[_Prov("ollama-local", "ollama", default_model="qwen:0.5b")]
    )
    ids = [m.model_id for m in await reg.list_available_models()]
    assert ids == ["llama3.1:8b", "mistral:7b"]
    assert "qwen:0.5b" not in ids


async def test_list_available_models_keeps_pinned_embedding_as_deprecated():
    """fetch_provider_models() drops embedding ids from the cached list, so
    a pinned one must resurface through the orphan-pin branch as
    deprecated=True rather than vanish — the operator needs to see the bad
    assignment (#1004). Seeds the real cache so the filter is exercised."""
    from core.llm.providers import registry as model_registry

    model_registry._MODEL_LIST_CACHE["ollama-local"] = [
        "llama3.1:8b",
        "nomic-embed-text:latest",
    ]
    reg = _StubRegistry(
        assignments={
            "triage": ComponentAssignment(
                component="triage",
                provider_id="ollama-local",
                model_id="nomic-embed-text:latest",
            ),
        },
        active_providers=[_Prov("ollama-local", "ollama", default_model="llama3.1:8b")],
    )
    try:
        by_id = {m.model_id: m for m in await reg.list_available_models()}
    finally:
        model_registry._MODEL_LIST_CACHE.pop("ollama-local", None)
    assert set(by_id) == {"llama3.1:8b", "nomic-embed-text:latest"}
    assert by_id["llama3.1:8b"].deprecated is False
    assert by_id["nomic-embed-text:latest"].deprecated is True


async def test_fallback_models_reflects_ollama_not_anthropic():
    """fallback_models() must return the configured provider's models
    (Ollama here), never a hardcoded Anthropic set (#409)."""
    reg = _StubRegistry(
        active_providers=[_Prov("ollama-local", "ollama", default_model="llama3.1:8b")]
    )
    models = await reg.fallback_models()
    ids = [m.model_id for m in models]
    assert "llama3.1:8b" in ids
    assert all(m.provider_type == "ollama" for m in models)
    assert not any(mid.startswith("claude") for mid in ids)


async def test_fallback_models_empty_when_no_providers():
    """No configured providers → empty, so the caller can apply its own
    last-resort default (the fresh-install Claude bootstrap in the API)."""
    reg = _StubRegistry(active_providers=[])
    assert await reg.fallback_models() == []
