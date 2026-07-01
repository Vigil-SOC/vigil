from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.configured_llm import (  # noqa: E402
    NoConfiguredLLMProvider,
    generate_configured_text,
    resolve_configured_llm,
)
from services.llm_router import ProviderSpec  # noqa: E402

pytestmark = pytest.mark.unit


def _ollama_spec(default_model: str = "qwen3.5:latest") -> ProviderSpec:
    return ProviderSpec(
        provider_id="ollama-local",
        provider_type="ollama",
        base_url="http://localhost:11434",
        api_key_ref=None,
        default_model=default_model,
        config={},
    )


def _anthropic_spec(default_model: str = "claude-sonnet-test") -> ProviderSpec:
    return ProviderSpec(
        provider_id="anthropic-default",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="ANTHROPIC_API_KEY",
        default_model=default_model,
        config={},
    )


class _Registry:
    def __init__(self, resolved):
        self.resolved = resolved

    def resolve_model_for_component(self, component):
        assert component == "reporting"
        return self.resolved


def _patch_registry(monkeypatch, resolved):
    import services.model_registry as model_registry

    monkeypatch.setattr(model_registry, "get_registry", lambda: _Registry(resolved))


def test_resolve_non_anthropic_rewrites_stale_claude_model(monkeypatch):
    _patch_registry(monkeypatch, ("ollama-local", "claude-sonnet-4-6"))

    import services.llm_router as llm_router

    monkeypatch.setattr(llm_router, "get_provider_spec", lambda pid: _ollama_spec())
    monkeypatch.setattr(llm_router, "get_default_provider_spec", lambda: None)

    selection = resolve_configured_llm("reporting")

    assert selection.provider_id == "ollama-local"
    assert selection.provider_type == "ollama"
    assert selection.model == "qwen3.5:latest"


@pytest.mark.asyncio
async def test_generate_non_anthropic_uses_router_without_claude_key(monkeypatch):
    _patch_registry(monkeypatch, ("ollama-local", "qwen3.5:latest"))

    import services.llm_router as llm_router

    monkeypatch.setattr(llm_router, "get_provider_spec", lambda pid: _ollama_spec())
    monkeypatch.setattr(llm_router, "get_default_provider_spec", lambda: None)

    calls = {}

    class _Router:
        async def dispatch(self, **kwargs):
            calls.update(kwargs)
            return {"content": "ok", "path": "bifrost", "provider": "ollama"}

    monkeypatch.setattr(llm_router, "LLMRouter", _Router)

    result = await generate_configured_text(
        message="hello",
        component="reporting",
        system_prompt="be terse",
        recommended_tools=[{"name": "get_finding"}],
    )

    assert result.content == "ok"
    assert result.provider_id == "ollama-local"
    assert result.provider_type == "ollama"
    assert result.model == "qwen3.5:latest"
    assert result.path == "bifrost"
    assert calls["provider"].provider_id == "ollama-local"
    assert calls["messages"] == [{"role": "user", "content": "hello"}]
    assert calls["tools"] is None
    assert "cannot execute tools" in calls["system_prompt"]


@pytest.mark.asyncio
async def test_generate_anthropic_uses_claude_service(monkeypatch):
    _patch_registry(monkeypatch, ("anthropic-default", "claude-sonnet-test"))

    import services.claude_service as claude_service
    import services.llm_router as llm_router

    monkeypatch.setattr(
        llm_router, "get_provider_spec", lambda pid: _anthropic_spec()
    )
    monkeypatch.setattr(llm_router, "get_default_provider_spec", lambda: None)

    calls = {}

    class _ClaudeService:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def has_api_key(self):
            return True

        def chat(self, **kwargs):
            calls["chat"] = kwargs
            return "anthropic ok"

    monkeypatch.setattr(claude_service, "ClaudeService", _ClaudeService)

    result = await generate_configured_text(
        message="hello",
        component="reporting",
        context=[{"role": "user", "content": "previous"}],
        system_prompt="be useful",
        recommended_tools=[{"name": "get_finding"}],
        use_backend_tools=True,
        use_mcp_tools=True,
        enable_thinking=True,
    )

    assert result.content == "anthropic ok"
    assert result.provider_id == "anthropic-default"
    assert result.provider_type == "anthropic"
    assert result.model == "claude-sonnet-test"
    assert result.path == "claude_service"
    assert calls["init"]["use_backend_tools"] is True
    assert calls["init"]["use_mcp_tools"] is True
    assert calls["init"]["enable_thinking"] is True
    assert calls["chat"]["model"] == "claude-sonnet-test"
    assert calls["chat"]["recommended_tools"] == [{"name": "get_finding"}]


@pytest.mark.asyncio
async def test_generate_anthropic_without_key_raises(monkeypatch):
    _patch_registry(monkeypatch, ("anthropic-default", "claude-sonnet-test"))

    import services.claude_service as claude_service
    import services.llm_router as llm_router

    monkeypatch.setattr(
        llm_router, "get_provider_spec", lambda pid: _anthropic_spec()
    )
    monkeypatch.setattr(llm_router, "get_default_provider_spec", lambda: None)

    class _ClaudeService:
        def __init__(self, **kwargs):
            pass

        def has_api_key(self):
            return False

    monkeypatch.setattr(claude_service, "ClaudeService", _ClaudeService)

    with pytest.raises(NoConfiguredLLMProvider):
        await generate_configured_text(message="hello", component="reporting")
