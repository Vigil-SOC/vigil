"""Unit tests for ``core.llm.cost.estimator`` (#184 Phase 2).

Covers registry-priced estimates for OpenAI, Vertex, Ollama and unknown
providers, the Anthropic ``count_tokens`` path via a mocked client, and the
``/analytics/estimate-cost`` provider resolution (#1118).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def seeded_rates():
    """Seed the registry the way the sync records a gateway datasheet read."""
    from core.llm.providers import registry as model_registry
    from core.llm.providers.discovery import ModelMeta

    def seed(provider_type, model_id, inp, out):
        model_registry.record_live_meta(
            provider_type,
            [
                ModelMeta(
                    model_id,
                    model_id,
                    input_cost_per_token=inp,
                    output_cost_per_token=out,
                )
            ],
            rates_only=True,
        )

    yield seed
    model_registry.clear_live_meta()


def test_openai_estimator_uses_registry_rates(seeded_rates):
    from core.llm.cost.estimator import estimate_cost

    seeded_rates("openai", "gpt-4o", 2.50 / 1_000_000, 10.0 / 1_000_000)
    est = _run(
        estimate_cost(
            provider_type="openai",
            model_id="gpt-4o",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=1000,
        )
    )
    in_rate = 2.50 / 1_000_000
    out_rate = 10.0 / 1_000_000

    assert est.provider_type == "openai"
    assert est.model_id == "gpt-4o"
    assert est.input_tokens > 0
    assert est.low_usd == pytest.approx(est.input_tokens * in_rate, rel=1e-9)
    assert est.high_usd == pytest.approx(
        est.input_tokens * in_rate + 1000 * out_rate, rel=1e-9
    )
    assert est.pricing_source == "exact"


def test_openai_estimator_falls_back_to_heuristic_when_tiktoken_missing():
    """If tiktoken isn't installed, the char heuristic kicks in and
    ``token_count_method`` reflects that. Forces the import to fail."""
    import builtins

    from core.llm.cost.estimator import estimate_cost

    real_import = builtins.__import__

    def _fail_tiktoken(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("tiktoken not installed in this test")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _fail_tiktoken):
        est = _run(
            estimate_cost(
                provider_type="openai",
                model_id="gpt-4o",
                messages=[{"role": "user", "content": "hello world"}],
            )
        )
    assert est.token_count_method == "char_heuristic"
    assert est.input_tokens >= 1


def test_ollama_estimator_returns_zero_cost():
    from core.llm.cost.estimator import estimate_cost

    est = _run(
        estimate_cost(
            provider_type="ollama",
            model_id="llama3.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert est.low_usd == 0.0
    assert est.high_usd == 0.0
    assert est.pricing_source == "zero"


def test_vertex_gemini_is_priced_from_the_registry(seeded_rates):
    from core.llm.cost.estimator import estimate_cost

    seeded_rates("vertex", "gemini-2.5-flash", 0.30 / 1_000_000, 2.50 / 1_000_000)
    est = _run(
        estimate_cost(
            provider_type="vertex",
            model_id="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hello there"}],
            max_tokens=1000,
        )
    )
    assert est.pricing_source not in ("unknown", "zero")
    assert est.token_count_method == "char_heuristic"
    assert est.high_usd > est.low_usd > 0


def test_unknown_provider_returns_zero_with_unknown_source():
    from core.llm.cost.estimator import estimate_cost

    est = _run(
        estimate_cost(
            provider_type="some-future-vendor",
            model_id="some-model",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert est.low_usd == 0.0
    assert est.high_usd == 0.0
    assert est.pricing_source == "unknown"


def test_unknown_provider_calls_record_pricing_unknown(monkeypatch):
    """Counter side-effect fires for the unknown branch (#184 acceptance #2)."""
    from core.llm.cost import estimator as cost_estimator

    calls = []
    monkeypatch.setattr(
        "core.llm.providers.registry._record_pricing_unknown",
        lambda provider_type, model_id: calls.append((provider_type, model_id)),
    )

    est = _run(
        cost_estimator.estimate_cost(
            provider_type="some-future-vendor",
            model_id="some-model",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert est.pricing_source == "unknown"
    assert calls == [("some-future-vendor", "some-model")]


def test_anthropic_estimator_uses_count_tokens_when_available(
    monkeypatch, seeded_rates
):
    """Mock the Bifrost-routed Anthropic client so the test doesn't
    depend on a real API key or network.

    Verifies that:
      1. count_tokens is called on the routed client
      2. token_count_method == "anthropic_count_tokens"
      3. the cost band is built from registry rates × returned count
    """
    from core.llm.cost import estimator as cost_estimator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seeded_rates(
        "anthropic", "claude-sonnet-4-5-20250929", 3.0 / 1_000_000, 15.0 / 1_000_000
    )

    class _FakeMessages:
        async def count_tokens(self, **kwargs):  # noqa: D401
            class _R:
                input_tokens = 1234

            return _R()

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    monkeypatch.setattr(
        "core.llm.providers.clients.create_async_anthropic_client",
        lambda api_key, timeout=None: _FakeClient(),
    )

    est = _run(
        cost_estimator.estimate_cost(
            provider_type="anthropic",
            model_id="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=2048,
        )
    )

    in_rate = 3.0 / 1_000_000
    out_rate = 15.0 / 1_000_000
    assert est.token_count_method == "anthropic_count_tokens"
    assert est.input_tokens == 1234
    assert est.low_usd == pytest.approx(1234 * in_rate, rel=1e-9)
    assert est.high_usd == pytest.approx(1234 * in_rate + 2048 * out_rate, rel=1e-9)
    assert est.pricing_source == "exact"


def test_anthropic_estimator_falls_back_when_count_tokens_raises(monkeypatch):
    """If the Bifrost-routed count_tokens call raises, we still return
    a useful estimate via the char heuristic instead of bubbling the error."""
    from core.llm.cost import estimator as cost_estimator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _BrokenMessages:
        async def count_tokens(self, **kwargs):
            raise RuntimeError("bifrost is sad")

    class _BrokenClient:
        def __init__(self):
            self.messages = _BrokenMessages()

    monkeypatch.setattr(
        "core.llm.providers.clients.create_async_anthropic_client",
        lambda api_key, timeout=None: _BrokenClient(),
    )

    est = _run(
        cost_estimator.estimate_cost(
            provider_type="anthropic",
            model_id="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": "hello world"}],
        )
    )
    assert est.token_count_method == "char_heuristic"
    assert est.input_tokens >= 1


# ---------------------------------------------------------------------------
# /analytics/estimate-cost provider resolution (#1118)
# ---------------------------------------------------------------------------


def _estimate_via_endpoint(monkeypatch, default_provider, model_id):
    """Hit the endpoint with a model the registry does not list, so the
    provider comes from the configured default rather than the model's name."""
    import core.llm.router.router as router_mod
    from services.api.routers import analytics

    class _Registry:
        async def list_available_models(self):
            return []

    monkeypatch.setattr(analytics, "get_registry", lambda: _Registry())
    monkeypatch.setattr(
        router_mod,
        "get_default_provider_spec",
        lambda: SimpleNamespace(provider_type=default_provider),
    )
    return _run(
        analytics.estimate_cost_endpoint(
            analytics.EstimateCostRequest(
                model_id=model_id, messages=[{"role": "user", "content": "hi"}]
            )
        )
    )


def test_bare_id_under_an_ollama_default_is_zero(monkeypatch):
    est = _estimate_via_endpoint(monkeypatch, "ollama", "qwen2.5-coder")
    assert est["provider_type"] == "ollama"
    assert est["pricing_source"] == "zero"


def test_bare_llama_under_a_commercial_default_is_not_zero(monkeypatch):
    est = _estimate_via_endpoint(monkeypatch, "groq", "llama-3.3-70b-versatile")
    assert est["provider_type"] == "groq"
    assert est["pricing_source"] != "zero"
