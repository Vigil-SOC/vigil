"""Unit tests for services.llm_router (GH #88).

Exercises the pure-logic path-selection rules and the dispatch wiring
with mocked openai / anthropic clients.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.llm_router import (
    LLMRouter,
    ProviderSpec,
    provider_spec_from_row,
    select_path,
)


pytestmark = pytest.mark.unit


def _anthropic_spec() -> ProviderSpec:
    return ProviderSpec(
        provider_id="anthropic-default",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="CLAUDE_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )


def _ollama_spec() -> ProviderSpec:
    return ProviderSpec(
        provider_id="ollama-local",
        provider_type="ollama",
        base_url="http://localhost:11434",
        api_key_ref=None,
        default_model="llama3.1:8b",
        config={},
    )


def _openai_spec() -> ProviderSpec:
    return ProviderSpec(
        provider_id="openai-prod",
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key_ref="llm_provider_openai-prod_api_key",
        default_model="gpt-4o-mini",
        config={},
    )


# ---------------------------------------------------------------------------
# Path selection (pure logic)
# ---------------------------------------------------------------------------


def test_path_anthropic_with_thinking_uses_bifrost():
    """GH #84 PR-B: all Anthropic traffic goes through Bifrost, even thinking."""
    assert select_path(_anthropic_spec(), enable_thinking=True) == "bifrost"


def test_path_anthropic_without_thinking_uses_bifrost():
    assert select_path(_anthropic_spec(), enable_thinking=False) == "bifrost"


def test_path_openai_always_uses_bifrost():
    assert select_path(_openai_spec(), enable_thinking=False) == "bifrost"
    assert select_path(_openai_spec(), enable_thinking=True) == "bifrost"


def test_path_ollama_always_uses_bifrost():
    assert select_path(_ollama_spec(), enable_thinking=True) == "bifrost"


def test_router_class_method_matches_free_function():
    spec = _anthropic_spec()
    router = LLMRouter()
    assert (
        router.select_path(spec, enable_thinking=True)
        == select_path(spec, enable_thinking=True)
    )


# ---------------------------------------------------------------------------
# Dispatch — Bifrost branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_bifrost_for_ollama():
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hello", tool_calls=None)
            )
        ],
        model="ollama/llama3.1:8b",
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7),
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client) as oai_ctor:
        out = await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="be terse",
        )
    oai_ctor.assert_called_once()
    # base_url must be the Bifrost URL the router was constructed with
    assert oai_ctor.call_args.kwargs["base_url"] == "http://test-bifrost:8080/v1"

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "ollama/llama3.1:8b"
    assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}

    assert out["path"] == "bifrost"
    assert out["provider"] == "ollama"
    assert out["content"] == "hello"
    assert out["input_tokens"] == 5
    assert out["output_tokens"] == 7


# ---------------------------------------------------------------------------
# Dispatch — Anthropic direct branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_anthropic_with_thinking_routes_through_bifrost():
    """GH #84 PR-B: Anthropic thinking calls use Bifrost's /anthropic passthrough.

    The Anthropic SDK is still the client the router builds, but its
    ``base_url`` points at Bifrost so extended thinking + prompt caching
    round-trip unchanged while Bifrost handles caching + observability.
    """
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    thinking_block = SimpleNamespace(type="thinking", thinking="inner reasoning")
    text_block = SimpleNamespace(type="text", text="the answer")
    fake_resp = SimpleNamespace(
        content=[thinking_block, text_block],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=34,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_resp)

    # Router builds its Anthropic client via services.llm_clients.create_async_anthropic_client,
    # which in turn instantiates anthropic.AsyncAnthropic with base_url=<bifrost>/anthropic.
    with patch("anthropic.AsyncAnthropic", return_value=mock_client) as ac_ctor, \
         patch("services.llm_router.get_secret", return_value="sk-ant-fake"), \
         patch.dict("os.environ", {"BIFROST_URL": "http://test-bifrost:8080"}):
        out = await router.dispatch(
            provider=_anthropic_spec(),
            messages=[{"role": "user", "content": "ponder"}],
            enable_thinking=True,
            thinking_budget=4096,
        )

    ac_ctor.assert_called_once_with(
        api_key="sk-ant-fake",
        base_url="http://test-bifrost:8080/anthropic",
        timeout=1800.0,
    )
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert kwargs["messages"] == [{"role": "user", "content": "ponder"}]

    assert out["path"] == "bifrost"
    assert out["provider"] == "anthropic"
    assert out["content"] == "the answer"
    assert out["thinking"] == "inner reasoning"
    assert out["input_tokens"] == 12
    assert out["output_tokens"] == 34
    assert out["cache_read_tokens"] == 0
    assert out["cache_creation_tokens"] == 0


@pytest.mark.asyncio
async def test_anthropic_dispatch_raises_when_no_key():
    router = LLMRouter()
    with patch("services.llm_router.get_secret", return_value=None), \
         patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "CLAUDE_API_KEY": ""}, clear=False):
        with pytest.raises(RuntimeError, match="no resolvable API key"):
            await router.dispatch(
                provider=_anthropic_spec(),
                messages=[{"role": "user", "content": "hi"}],
                enable_thinking=True,
            )


# ---------------------------------------------------------------------------
# provider_spec_from_row
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Non-default Anthropic providers must route through the router so the
# per-provider api_key_ref is resolved (regression for PR #103 review).
# ---------------------------------------------------------------------------


def test_is_default_anthropic_recognizes_legacy_refs():
    from services.llm_worker import _is_default_anthropic_spec

    default_key = ProviderSpec(
        provider_id="anthropic-default",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="CLAUDE_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )
    legacy_key = ProviderSpec(
        provider_id="anthropic-default",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )
    per_provider = ProviderSpec(
        provider_id="anthropic-team",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="llm_provider_anthropic-team_api_key",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )
    no_ref = ProviderSpec(
        provider_id="anthropic-anon",
        provider_type="anthropic",
        base_url=None,
        api_key_ref=None,
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )
    openai = _openai_spec()
    assert _is_default_anthropic_spec(default_key) is True
    assert _is_default_anthropic_spec(legacy_key) is True
    assert _is_default_anthropic_spec(no_ref) is True  # falls back to env
    assert _is_default_anthropic_spec(per_provider) is False
    assert _is_default_anthropic_spec(openai) is False


@pytest.mark.asyncio
async def test_non_default_anthropic_with_thinking_dispatches_via_router(monkeypatch):
    """PR #103 review regression: a non-default Anthropic provider with
    enable_thinking=True must be dispatched via LLMRouter so the
    per-provider api_key_ref is used, NOT the shared ClaudeService
    whose key is CLAUDE_API_KEY.
    """
    from services.llm_worker import _maybe_dispatch_via_router

    per_provider = ProviderSpec(
        provider_id="anthropic-team",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="llm_provider_anthropic-team_api_key",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )

    mock_router = MagicMock()
    mock_router.select_path = MagicMock(return_value="bifrost")
    mock_router.dispatch = AsyncMock(return_value={
        "content": "ok", "path": "bifrost", "provider": "anthropic",
        "input_tokens": 1, "output_tokens": 1, "model": "x",
    })

    import asyncio
    ctx = {
        "llm_router": mock_router,
        "rate_limiter": asyncio.Semaphore(1),
    }

    with patch(
        "services.llm_router.get_provider_spec",
        return_value=per_provider,
    ):
        result = await _maybe_dispatch_via_router(
            ctx,
            provider_id="anthropic-team",
            messages=[{"role": "user", "content": "think hard"}],
            system_prompt=None,
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
            temperature=None,
            tools=None,
            enable_thinking=True,
            thinking_budget=4096,
        )

    # We MUST have dispatched via the router (not returned None which would
    # fall back to the shared ClaudeService with the wrong key).
    assert result is not None
    mock_router.dispatch.assert_awaited_once()
    dispatch_kwargs = mock_router.dispatch.call_args.kwargs
    assert dispatch_kwargs["provider"].api_key_ref == (
        "llm_provider_anthropic-team_api_key"
    )
    assert dispatch_kwargs["enable_thinking"] is True


@pytest.mark.asyncio
async def test_default_anthropic_with_thinking_still_falls_back():
    """Default Anthropic row with thinking=True should keep using the
    shared ClaudeService (return None), preserving prompt caching and
    the tool-use loop that lives there.
    """
    from services.llm_worker import _maybe_dispatch_via_router

    default_spec = ProviderSpec(
        provider_id="anthropic-default",
        provider_type="anthropic",
        base_url=None,
        api_key_ref="CLAUDE_API_KEY",
        default_model="claude-sonnet-4-5-20250929",
        config={},
    )

    mock_router = MagicMock()
    mock_router.select_path = MagicMock(return_value="bifrost")
    mock_router.dispatch = AsyncMock()

    import asyncio
    ctx = {
        "llm_router": mock_router,
        "rate_limiter": asyncio.Semaphore(1),
    }
    with patch(
        "services.llm_router.get_provider_spec",
        return_value=default_spec,
    ):
        result = await _maybe_dispatch_via_router(
            ctx,
            provider_id="anthropic-default",
            messages=[{"role": "user", "content": "think hard"}],
            system_prompt=None,
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
            temperature=None,
            tools=None,
            enable_thinking=True,
            thinking_budget=4096,
        )
    assert result is None
    mock_router.dispatch.assert_not_awaited()


def test_provider_spec_from_row_copies_fields():
    row = SimpleNamespace(
        provider_id="p",
        provider_type="openai",
        base_url="https://example.com",
        api_key_ref="ref",
        default_model="gpt-4o",
        config={"organization": "o"},
    )
    spec = provider_spec_from_row(row)
    assert spec.provider_id == "p"
    assert spec.provider_type == "openai"
    assert spec.base_url == "https://example.com"
    assert spec.api_key_ref == "ref"
    assert spec.default_model == "gpt-4o"
    assert spec.config == {"organization": "o"}
