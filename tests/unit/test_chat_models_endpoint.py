"""Unit tests for the chat model picker endpoint `GET /claude/models` (GH #409).

The bug: the dropdown only listed Anthropic models on instances that run
other providers (e.g. Ollama via Bifrost). These tests pin the endpoint's
provider-aware behaviour: live models pass through, an empty live list falls
back to the *configured* providers (not hardcoded Claude), and the hardcoded
Claude bootstrap only appears when nothing is configured at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from backend.api import claude as claude_api  # noqa: E402
from services.model_registry import ModelRegistry  # noqa: E402

pytestmark = pytest.mark.unit


def _mi(provider_type, model_id, provider_id=None):
    return ModelRegistry.get_model_info(
        provider_id=provider_id or f"{provider_type}-default",
        provider_type=provider_type,
        model_id=model_id,
    )


class _FakeRegistry:
    def __init__(self, live=None, fallback=None):
        self._live = live or []
        self._fallback = fallback or []

    async def list_available_models(self):
        return list(self._live)

    async def fallback_models(self):
        return list(self._fallback)


async def test_get_models_returns_live_non_anthropic(monkeypatch):
    reg = _FakeRegistry(
        live=[_mi("ollama", "llama3.1:8b"), _mi("ollama", "mistral:7b")]
    )
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = [m["id"] for m in out["models"]]
    assert ids == ["llama3.1:8b", "mistral:7b"]
    assert not any(i.startswith("claude") for i in ids)


async def test_get_models_uses_provider_fallback_not_claude(monkeypatch):
    # Live discovery empty, but an Ollama provider IS configured → the picker
    # must reflect Ollama, not the hardcoded Claude bootstrap (#409).
    reg = _FakeRegistry(live=[], fallback=[_mi("ollama", "llama3.1:8b")])
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = [m["id"] for m in out["models"]]
    assert ids == ["llama3.1:8b"]
    assert not any(i.startswith("claude") for i in ids)


async def test_get_models_hardcoded_claude_only_when_nothing_configured(monkeypatch):
    # No live models AND no configured providers → last-resort Claude bootstrap
    # so a truly fresh install still renders a picker.
    reg = _FakeRegistry(live=[], fallback=[])
    monkeypatch.setattr(claude_api, "get_registry", lambda: reg)
    out = await claude_api.get_models()
    ids = [m["id"] for m in out["models"]]
    assert ids  # non-empty
    assert all(i.startswith("claude") for i in ids)
    assert "claude-sonnet-4-6" in ids
