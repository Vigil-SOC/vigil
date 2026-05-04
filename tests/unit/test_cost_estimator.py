"""Unit tests for ``services.cost_estimator`` (#184 Phase 2).

Covers the synchronous OpenAI / Ollama / unknown branches and the
Anthropic branch's fallback path (no API key set → char heuristic).
The Anthropic ``count_tokens`` API path is tested via mocked client.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_openai_estimator_uses_registry_rates():
    from services.cost_estimator import estimate_openai

    est = estimate_openai(
        model_id="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1000,
    )
    # gpt-4o has exact pricing: $2.50/MTok in, $10/MTok out.
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

    from services.cost_estimator import estimate_openai

    real_import = builtins.__import__

    def _fail_tiktoken(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("tiktoken not installed in this test")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _fail_tiktoken):
        est = estimate_openai(
            model_id="gpt-4o",
            messages=[{"role": "user", "content": "hello world"}],
        )
    assert est.token_count_method == "char_heuristic"
    assert est.input_tokens >= 1


def test_ollama_estimator_returns_zero_cost():
    from services.cost_estimator import estimate_ollama

    est = estimate_ollama(
        model_id="llama3.1",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert est.low_usd == 0.0
    assert est.high_usd == 0.0
    assert est.pricing_source == "zero"


def test_unknown_provider_returns_zero_with_unknown_source():
    from services.cost_estimator import estimate_cost

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


def test_anthropic_estimator_uses_count_tokens_when_available(monkeypatch):
    """Mock the Bifrost-routed Anthropic client so the test doesn't
    depend on a real API key or network.

    Verifies that:
      1. count_tokens is called on the routed client
      2. token_count_method == "anthropic_count_tokens"
      3. the cost band is built from registry rates × returned count
    """
    from services import cost_estimator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _FakeMessages:
        async def count_tokens(self, **kwargs):  # noqa: D401
            class _R:
                input_tokens = 1234

            return _R()

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    monkeypatch.setattr(
        "services.llm_clients.create_async_anthropic_client",
        lambda api_key, timeout=None: _FakeClient(),
    )

    est = _run(
        cost_estimator.estimate_anthropic(
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
    from services import cost_estimator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _BrokenMessages:
        async def count_tokens(self, **kwargs):
            raise RuntimeError("bifrost is sad")

    class _BrokenClient:
        def __init__(self):
            self.messages = _BrokenMessages()

    monkeypatch.setattr(
        "services.llm_clients.create_async_anthropic_client",
        lambda api_key, timeout=None: _BrokenClient(),
    )

    est = _run(
        cost_estimator.estimate_anthropic(
            model_id="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": "hello world"}],
        )
    )
    assert est.token_count_method == "char_heuristic"
    assert est.input_tokens >= 1
