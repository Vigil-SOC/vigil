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

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.llm_router import (LLMRouter, ProviderSpec,
                                 provider_spec_from_row, select_path)

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
    assert router.select_path(spec, enable_thinking=True) == select_path(
        spec, enable_thinking=True
    )


# ---------------------------------------------------------------------------
# Dispatch — Bifrost branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_bifrost_for_ollama():
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="hello", tool_calls=None))
        ],
        model="ollama/llama3.1:8b",
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
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
    # The OpenAI-format dispatcher must close its client (no httpx pool leak).
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_bifrost_translates_anthropic_tools_and_messages():
    """The daemon builds tools/messages in Anthropic shape. The OpenAI Bifrost
    dispatch must translate both (input_schema->parameters, tool_use->tool_calls,
    tool_result->role:tool) and normalize the response tool_calls back to
    {id,name,input} dicts, or the daemon's multi-turn tool loop breaks on
    non-Anthropic providers.
    """
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    returned_tc = SimpleNamespace(
        id="call_9",
        function=SimpleNamespace(name="get_case", arguments='{"case_id": "C1"}'),
    )
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[returned_tc])
            )
        ],
        model="ollama/llama3.1:8b",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    anthropic_tools = [
        {"name": "get_case", "description": "d", "input_schema": {"type": "object"}}
    ]
    anthropic_messages = [
        {"role": "user", "content": "investigate"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_9", "name": "get_case", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_9", "content": "case data"}
            ],
        },
    ]

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        out = await router.dispatch(
            provider=_ollama_spec(),
            messages=anthropic_messages,
            tools=anthropic_tools,
        )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    # Tools translated to OpenAI function shape.
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["name"] == "get_case"
    assert kwargs["tools"][0]["function"]["parameters"] == {"type": "object"}
    # Messages translated: user text, assistant tool_calls, tool result message.
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert kwargs["messages"][1]["tool_calls"][0]["id"] == "call_9"
    assert kwargs["messages"][2]["tool_call_id"] == "call_9"
    # Response tool_calls normalized to {id,name,input} dicts.
    assert out["tool_calls"] == [
        {"id": "call_9", "name": "get_case", "input": {"case_id": "C1"}}
    ]


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
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake_resp)

    # Router builds its Anthropic client via services.llm_clients.create_async_anthropic_client,
    # which in turn instantiates anthropic.AsyncAnthropic with base_url=<bifrost>/anthropic.
    with patch("anthropic.AsyncAnthropic", return_value=mock_client) as ac_ctor, patch(
        "services.llm_router.get_secret", return_value="sk-ant-fake"
    ), patch.dict("os.environ", {"BIFROST_URL": "http://test-bifrost:8080"}):
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
    # The Anthropic dispatcher must close its client (no httpx pool leak).
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_bifrost_openai_extracts_cache_read_tokens():
    """#184 acceptance #2: OpenAI prompt-cache tokens were dropped on the floor
    by the dispatch layer, leaving cache hits billed at full input rate. Verify
    `usage.prompt_tokens_details.cached_tokens` is now read into
    `cache_read_tokens` (and `cache_creation_tokens` stays 0 — OpenAI doesn't
    bill cache creation as a separate tier the way Anthropic does).
    """
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="cached!", tool_calls=None))
        ],
        model="openai/gpt-4o",
        usage=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=200,
            prompt_tokens_details=SimpleNamespace(cached_tokens=750),
        ),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        out = await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )
    assert out["input_tokens"] == 1000
    assert out["output_tokens"] == 200
    assert out["cache_read_tokens"] == 750
    assert out["cache_creation_tokens"] == 0


@pytest.mark.asyncio
async def test_dispatch_bifrost_openai_no_cache_details_safe():
    """When prompt_tokens_details is missing (older OpenAI responses or models
    without cache support), cache_read_tokens defaults to 0 — must not raise.
    """
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="x", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        # no prompt_tokens_details attribute
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        out = await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )
    assert out["cache_read_tokens"] == 0
    assert out["cache_creation_tokens"] == 0


@pytest.mark.asyncio
async def test_dispatch_propagates_interaction_id_as_bifrost_log_header_openai():
    """#185: each LLM call carries an `x-bf-lh-vigil-interaction-id` header
    so Bifrost's logging plugin can correlate the LogEntry back to Vigil's
    local LLMInteractionLog row by UUID. The `x-bf-lh-*` prefix is
    Bifrost's logging-headers convention — anything with that prefix gets
    captured into LogEntry.metadata."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    interaction_id = "uuid-aaaa-1111"
    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id=interaction_id,
        )

    headers = mock_client.chat.completions.create.call_args.kwargs.get("extra_headers")
    assert headers is not None
    assert headers.get("x-bf-lh-vigil-interaction-id") == interaction_id


@pytest.mark.asyncio
async def test_dispatch_omits_extra_headers_when_no_interaction_id():
    """No interaction_id passed → no extra_headers kwarg, so we don't
    accidentally inject empty headers into every call."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "extra_headers" not in kwargs


@pytest.mark.asyncio
async def test_dispatch_propagates_interaction_id_anthropic():
    """Same correlation header on the Anthropic dispatch path."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake_resp)

    interaction_id = "uuid-bbbb-2222"
    with patch("anthropic.AsyncAnthropic", return_value=mock_client), patch(
        "services.llm_router.get_secret", return_value="sk-ant-fake"
    ):
        await router.dispatch(
            provider=_anthropic_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id=interaction_id,
        )

    headers = mock_client.messages.create.call_args.kwargs.get("extra_headers")
    assert headers is not None
    assert headers.get("x-bf-lh-vigil-interaction-id") == interaction_id


@pytest.mark.asyncio
async def test_dispatch_attaches_vk_header_when_budget_enforce_active():
    """#186: when budget_service.should_enforce() is True and a VK is
    configured, dispatch must attach `x-bf-vk: <vk>` so Bifrost's
    governance layer enforces the budget upstream of the call."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client), patch(
        "services.budget_service.should_enforce", return_value=True
    ), patch("services.budget_service.get_active_vk", return_value="sk-bf-test-vk"):
        await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )

    headers = mock_client.chat.completions.create.call_args.kwargs.get("extra_headers")
    assert headers is not None
    assert headers.get("x-bf-vk") == "sk-bf-test-vk"


@pytest.mark.asyncio
async def test_dispatch_omits_vk_header_when_enforcement_off():
    """DEV_MODE / LLM_BUDGET_UNLIMITED → should_enforce() is False →
    don't attach x-bf-vk so Bifrost's bootstrap (no-VK) path applies."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client), patch(
        "services.budget_service.should_enforce", return_value=False
    ), patch("services.budget_service.get_active_vk", return_value="sk-bf-test-vk"):
        await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    # No interaction_id and no enforcement → no extra_headers at all.
    assert "extra_headers" not in kwargs


@pytest.mark.asyncio
async def test_dispatch_translates_402_into_budget_exceeded():
    """Bifrost returns 402 when the VK budget is exhausted. The router
    must translate that into the typed BudgetExceeded so the chat UI
    can render a banner instead of a 500 toast."""
    from services.budget_service import BudgetExceeded

    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    err = SimpleNamespace(status_code=402, message="$5 of $5 spent")
    raise_err = type("FakeAPIErr", (Exception,), {})("budget hit")
    raise_err.status_code = 402  # type: ignore[attr-defined]
    raise_err.message = "$5 of $5 spent"  # type: ignore[attr-defined]

    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=raise_err)

    with patch("openai.AsyncOpenAI", return_value=mock_client), patch(
        "services.budget_service.should_enforce", return_value=True
    ), patch("services.budget_service.get_active_vk", return_value="sk-bf-test"):
        with pytest.raises(BudgetExceeded) as excinfo:
            await router.dispatch(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "hi"}],
            )

    assert excinfo.value.status_code == 402
    assert excinfo.value.tier == "virtual_key"
    # Even on the error path the client must be closed (finally block).
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_translates_429_into_budget_exceeded_rate_tier():
    from services.budget_service import BudgetExceeded

    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    raise_err = type("FakeAPIErr", (Exception,), {})("rate limited")
    raise_err.status_code = 429  # type: ignore[attr-defined]

    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=raise_err)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        with pytest.raises(BudgetExceeded) as excinfo:
            await router.dispatch(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "hi"}],
            )

    assert excinfo.value.status_code == 429
    assert excinfo.value.tier == "rate_limit"


@pytest.mark.asyncio
async def test_dispatch_does_not_swallow_non_budget_errors():
    """Only 402/429 map to BudgetExceeded. A 500 should propagate as-is
    so the caller sees the real error and doesn't think it's a budget
    issue."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    raise_err = type("FakeAPIErr", (Exception,), {})("upstream blew up")
    raise_err.status_code = 500  # type: ignore[attr-defined]

    mock_client = MagicMock()
    # Dispatchers close the client in a finally block to avoid leaking the
    # httpx pool; make .close() awaitable so the real cleanup path runs.
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=raise_err)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        with pytest.raises(Exception) as excinfo:
            await router.dispatch(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "hi"}],
            )
    assert getattr(excinfo.value, "status_code", None) == 500
    # Must not have been wrapped into BudgetExceeded.
    from services.budget_service import BudgetExceeded

    assert not isinstance(excinfo.value, BudgetExceeded)


@pytest.mark.asyncio
async def test_anthropic_dispatch_raises_when_no_key():
    router = LLMRouter()
    with patch("services.llm_router.get_secret", return_value=None), patch.dict(
        "os.environ", {"ANTHROPIC_API_KEY": "", "CLAUDE_API_KEY": ""}, clear=False
    ):
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
    mock_router.dispatch = AsyncMock(
        return_value={
            "content": "ok",
            "path": "bifrost",
            "provider": "anthropic",
            "input_tokens": 1,
            "output_tokens": 1,
            "model": "x",
        }
    )

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
    assert spec.config == {"organization": "o"}


# ---------------------------------------------------------------------------
# discover_anthropic_api_key — fallback path so the chat drawer works for
# users who only configured Anthropic through the Settings UI (#292).
# ---------------------------------------------------------------------------


def _stub_session(rows):
    """Build a fake SQLAlchemy session that returns *rows* from .query(...).all()."""
    session = MagicMock()
    chain = session.query.return_value.filter.return_value.order_by.return_value
    chain.all.return_value = rows
    return session


def test_discover_anthropic_api_key_returns_secret_for_default_row():
    from services import llm_router

    default_row = SimpleNamespace(
        provider_id="anthropic-default",
        api_key_ref="llm_provider_anthropic-default_api_key",
    )
    session = _stub_session([default_row])

    with patch.object(llm_router, "get_secret", return_value="sk-ant-ui-saved"), patch(
        "database.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() == "sk-ant-ui-saved"


def test_discover_anthropic_api_key_falls_through_to_active_row():
    """If the default row's secret is missing, the next active row wins."""
    from services import llm_router

    default_row = SimpleNamespace(
        provider_id="anthropic-default",
        api_key_ref="llm_provider_anthropic-default_api_key",
    )
    other_row = SimpleNamespace(
        provider_id="anthropic-team",
        api_key_ref="llm_provider_anthropic-team_api_key",
    )
    session = _stub_session([default_row, other_row])

    def fake_get_secret(ref):
        # Default row's secret missing; team's secret resolves.
        return None if "default" in ref else "sk-ant-team-key"

    with patch.object(llm_router, "get_secret", side_effect=fake_get_secret), patch(
        "database.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() == "sk-ant-team-key"


def test_discover_anthropic_api_key_returns_none_when_no_rows():
    from services import llm_router

    session = _stub_session([])
    with patch.object(llm_router, "get_secret", return_value=None), patch(
        "database.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() is None


def test_discover_anthropic_api_key_returns_none_when_db_unavailable():
    """DB import error => silent None, so the legacy chain stays usable
    in environments where database.connection can't import."""
    # Patch ``get_db_session`` to raise on import. Easiest: make the
    # entire ``database.connection`` import fail by patching builtins.
    import builtins

    from services import llm_router

    real_import = builtins.__import__

    def boom_import(name, *args, **kwargs):
        if name == "database.connection":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=boom_import):
        assert llm_router.discover_anthropic_api_key() is None


# ---------------------------------------------------------------------------
# Streaming — dispatch_openai_stream / stream_openai_raw (GH #325, #436)
# ---------------------------------------------------------------------------


def _delta_chunk(content):
    """A minimal OpenAI streaming chunk carrying a content delta."""
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))]
    )


async def _achunks(chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_dispatch_openai_stream_yields_text_and_skips_empty():
    """The chat SSE path streams non-Anthropic providers through Bifrost's
    OpenAI-compatible /v1 endpoint. Text deltas must surface as
    {"type": "text", "content": ...}; frames with no choices or no content
    (e.g. role/usage-only frames) must be skipped."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    chunks = [
        _delta_chunk("Hello"),
        SimpleNamespace(choices=[]),  # no choices -> skipped
        _delta_chunk(None),  # no content -> skipped
        _delta_chunk(", world"),
    ]
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_achunks(chunks))

    with patch("openai.AsyncOpenAI", return_value=mock_client) as oai_ctor:
        out = [
            ev
            async for ev in router.dispatch_openai_stream(
                provider=_ollama_spec(),
                messages=[{"role": "user", "content": "hi"}],
                system_prompt="be terse",
            )
        ]

    assert out == [
        {"type": "text", "content": "Hello"},
        {"type": "text", "content": ", world"},
    ]
    # base_url must be Bifrost's OpenAI-compatible /v1 endpoint
    assert oai_ctor.call_args.kwargs["base_url"] == "http://test-bifrost:8080/v1"
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True
    # model is provider-prefixed so Bifrost routes to the right backend
    assert kwargs["model"] == "ollama/llama3.1:8b"
    assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
    # client is closed on normal completion (no httpx pool leak)
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_openai_raw_include_usage_sets_stream_options():
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_achunks([]))

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        _ = [
            c
            async for c in router.stream_openai_raw(
                provider=_ollama_spec(),
                messages=[{"role": "user", "content": "hi"}],
                include_usage=True,
            )
        ]

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_openai_raw_closes_client_on_early_disconnect():
    """If the SSE consumer goes away mid-stream, GeneratorExit propagates into
    stream_openai_raw's yield and the finally must still close the client so
    the httpx pool doesn't leak under load."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")

    async def _endless():
        for i in range(100):  # far more than the consumer will read
            yield _delta_chunk(f"tok{i}")

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_endless())

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        agen = router.stream_openai_raw(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )
        first = await agen.__anext__()
        assert getattr(first.choices[0].delta, "content", None) == "tok0"
        await agen.aclose()  # simulate consumer disconnect mid-stream

    mock_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_active_provider_spec (#325) — "which provider is live now", any type,
# not preferring is_default; None on DB error / no active row.
# ---------------------------------------------------------------------------
def _stub_first_session(row):
    """Fake session whose .query(...).filter(...).order_by(...).first() -> row."""
    session = MagicMock()
    chain = session.query.return_value.filter.return_value.order_by.return_value
    chain.first.return_value = row
    return session


def test_get_active_provider_spec_returns_first_active_row():
    from services import llm_router

    row = SimpleNamespace(
        provider_id="ollama-local",
        provider_type="ollama",
        base_url="http://localhost:11434",
        api_key_ref=None,
        default_model="llama3.1:8b",
        config={},
    )
    session = _stub_first_session(row)
    with patch("database.connection.get_db_session", return_value=session):
        spec = llm_router.get_active_provider_spec()
    assert spec is not None
    assert spec.provider_id == "ollama-local"
    assert spec.provider_type == "ollama"


def test_get_active_provider_spec_returns_none_when_no_active_row():
    from services import llm_router

    session = _stub_first_session(None)
    with patch("database.connection.get_db_session", return_value=session):
        assert llm_router.get_active_provider_spec() is None


def test_get_active_provider_spec_returns_none_on_db_error():
    from services import llm_router

    session = MagicMock()
    session.query.side_effect = RuntimeError("db down")
    with patch("database.connection.get_db_session", return_value=session):
        assert llm_router.get_active_provider_spec() is None
    # Even on error the session is closed (no leak).
    session.close.assert_called_once()


# ---------------------------------------------------------------------------
# dispatch() thin persistence (#413 PR3c-2 / risk R3)
#
# Before 3c-2 the router's single-turn dispatch() path wrote NO analytics row
# (LLMInteractionLog was only written on the engine paths and the worker's
# SDK-fallback path). These tests pin the new behaviour: every dispatch() —
# chat, findings enrichment, and the daemon/worker router path — persists a
# THIN row (provider/model/tokens/cost/interaction_id), priced by the *real*
# provider, and can never break the request path when the DB is unavailable.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def captured_interaction_rows():
    """Keep every dispatch test hermetic and expose persisted rows.

    dispatch() now writes an LLMInteractionLog via
    ``database.connection.get_db_manager().session_scope()``. Stub that handle
    so no dispatch test touches a real database, and hand back the list of rows
    added so the persistence tests can assert on them.
    """
    rows: list = []

    class _Session:
        def add(self, row):
            rows.append(row)

    class _Scope:
        def __enter__(self):
            return _Session()

        def __exit__(self, *exc):
            return False

    class _Manager:
        def session_scope(self):
            return _Scope()

    with patch("database.connection.get_db_manager", return_value=_Manager()):
        yield rows


def _bifrost_resp(content="hi there", model="ollama/llama3.1:8b", pt=11, ct=22):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
        ],
        model=model,
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


def _mock_openai_client(resp):
    client = MagicMock()
    client.close = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_dispatch_persists_thin_row_bifrost(captured_interaction_rows):
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp())
    with patch("openai.AsyncOpenAI", return_value=client):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id="iid-bifrost-1",
        )

    assert len(captured_interaction_rows) == 1
    row = captured_interaction_rows[0]
    assert row.interaction_id == "iid-bifrost-1"
    assert row.model == "ollama/llama3.1:8b"
    assert row.input_tokens == 11
    assert row.output_tokens == 22
    assert row.response_content == "hi there"


@pytest.mark.asyncio
async def test_dispatch_persists_thin_row_anthropic(captured_interaction_rows):
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="the answer")],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=34,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=0,
        ),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), patch(
        "services.llm_router.get_secret", return_value="sk-ant-fake"
    ), patch.dict("os.environ", {"BIFROST_URL": "http://test-bifrost:8080"}):
        await router.dispatch(
            provider=_anthropic_spec(),
            messages=[{"role": "user", "content": "ponder"}],
            interaction_id="iid-anthropic-1",
        )

    # R3 must close for ALL providers, not just the Bifrost/OpenAI branch.
    assert len(captured_interaction_rows) == 1
    row = captured_interaction_rows[0]
    assert row.interaction_id == "iid-anthropic-1"
    assert row.model == "claude-sonnet-4-5-20250929"
    assert row.input_tokens == 12
    assert row.output_tokens == 34
    assert row.cache_read_tokens == 5


@pytest.mark.asyncio
async def test_dispatch_generates_interaction_id_when_caller_omits_it(
    captured_interaction_rows,
):
    """findings.py enrichment calls dispatch() with no interaction_id. The row
    must still get a unique id (fresh UUID) so analytics never drops the call."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp())
    with patch("openai.AsyncOpenAI", return_value=client):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )

    row = captured_interaction_rows[0]
    # A uuid4 string is 36 chars; the point is simply "non-empty and unique".
    assert isinstance(row.interaction_id, str) and len(row.interaction_id) >= 32


@pytest.mark.asyncio
async def test_dispatch_prices_row_with_real_provider_not_hardcoded_anthropic(
    captured_interaction_rows,
):
    """The legacy rich helper hardcodes 'anthropic' into compute_call_cost; an
    Ollama/OpenAI row priced that way is wrong. The router writer must pass the
    dispatch result's real provider_type."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp())
    ccc = MagicMock(return_value=0.0)
    with patch("openai.AsyncOpenAI", return_value=client), patch(
        "daemon.agent_runner.compute_call_cost", ccc
    ):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id="iid-cost",
        )

    ccc.assert_called_once()
    # compute_call_cost(model, provider_type, input, output, ...)
    assert ccc.call_args.args[1] == "ollama"


@pytest.mark.asyncio
async def test_dispatch_persist_failure_is_non_fatal(captured_interaction_rows):
    """Persistence is fire-and-forget: a DB error must never break dispatch."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp(content="still works"))
    with patch("openai.AsyncOpenAI", return_value=client), patch(
        "database.connection.get_db_manager", side_effect=RuntimeError("db down")
    ):
        out = await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id="iid-fail",
        )

    assert out["content"] == "still works"
    assert captured_interaction_rows == []  # nothing persisted, no crash


@pytest.mark.asyncio
async def test_dispatch_row_is_thin_no_request_body(captured_interaction_rows):
    """'Thin' means we do not duplicate the heavy request body — request_messages
    stays empty and system_prompt None. The rich engine rows keep the full body."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp())
    with patch("openai.AsyncOpenAI", return_value=client):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "secret payload"}],
            system_prompt="a long system prompt",
            interaction_id="iid-thin",
        )

    row = captured_interaction_rows[0]
    assert row.request_messages == []
    assert row.system_prompt is None


@pytest.mark.asyncio
async def test_dispatch_row_omits_vk_when_enforcement_off(captured_interaction_rows):
    """virtual_key_id must be NULL when budget enforcement is off (DEV_MODE /
    LLM_BUDGET_UNLIMITED): no x-bf-vk header is sent, so no VK serviced the
    call and the row must not misattribute spend to a default VK."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    client = _mock_openai_client(_bifrost_resp())
    with patch("openai.AsyncOpenAI", return_value=client), patch(
        "services.budget_service.should_enforce", return_value=False
    ), patch(
        "services.budget_service.get_active_vk", return_value="vk-should-be-ignored"
    ):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
            interaction_id="iid-vk",
        )

    assert captured_interaction_rows[0].virtual_key_id is None


# ---------------------------------------------------------------------------
# Agent SDK passthrough (#413 PR3d)
#
# The Claude Agent SDK path (run_agent_task + the streaming agent_query) is
# Anthropic-only and lives in ClaudeService. 3d folds it behind LLMRouter as a
# behaviour-preserving passthrough so callers stop importing ClaudeService
# (caller migration itself is PR4). The router builds the SDK engine per call
# with use_agent_sdk=True and defaults matching today's callers; construction
# flags are overridable via agent_config so PR4 stays a mechanical swap.
# ---------------------------------------------------------------------------


class _FakeSDKEngine:
    """Stand-in for ClaudeService(use_agent_sdk=True) — records construction
    kwargs and delegated calls so the passthrough tests can assert fidelity."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.task_calls = []
        self.query_kwargs = None

    async def run_agent_task(self, task, agent_config=None, session_id=None):
        self.task_calls.append((task, agent_config, session_id))
        return {
            "task": task,
            "tool_calls": [{"tool": "get_case", "input": {}}],
            "final_result": "investigation complete",
            "success": True,
        }

    async def agent_query(
        self,
        prompt,
        system_prompt=None,
        allowed_tools=None,
        max_turns=10,
        session_id=None,
        model="unset",
    ):
        self.query_kwargs = dict(
            prompt=prompt,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            session_id=session_id,
            model=model,
        )
        for ev in (
            {"type": "tool_use", "tool": "get_case"},
            {"type": "result", "content": "done"},
        ):
            yield ev


def _patch_sdk_engine():
    """Patch the router's lazily-imported ClaudeService with a factory that
    stashes the constructed fake engine for inspection."""
    holder: dict = {}

    def factory(**kwargs):
        engine = _FakeSDKEngine(**kwargs)
        holder["engine"] = engine
        return engine

    return holder, patch("services.claude_service.ClaudeService", factory)


@pytest.mark.asyncio
async def test_run_agent_task_delegates_to_anthropic_sdk_engine():
    holder, p = _patch_sdk_engine()
    with p:
        out = await LLMRouter().run_agent_task(
            task="triage finding F1",
            agent_config={"system_prompt": "be careful", "model": "claude-x"},
            session_id="s1",
        )

    # Contract dict returned unchanged.
    assert out["success"] is True
    assert out["final_result"] == "investigation complete"
    assert out["tool_calls"] == [{"tool": "get_case", "input": {}}]
    # Engine built with the Agent SDK on; delegation forwarded verbatim.
    assert holder["engine"].init_kwargs["use_agent_sdk"] is True
    assert holder["engine"].task_calls == [
        (
            "triage finding F1",
            {"system_prompt": "be careful", "model": "claude-x"},
            "s1",
        )
    ]


@pytest.mark.asyncio
async def test_run_agent_task_construction_flags_come_from_config():
    holder, p = _patch_sdk_engine()
    with p:
        await LLMRouter().run_agent_task(
            task="t",
            agent_config={
                "use_mcp_tools": True,
                "enable_thinking": True,
                "use_backend_tools": True,
            },
        )
    k = holder["engine"].init_kwargs
    assert k["use_mcp_tools"] is True
    assert k["enable_thinking"] is True
    assert k["use_backend_tools"] is True
    assert k["use_agent_sdk"] is True


@pytest.mark.asyncio
async def test_run_agent_task_defaults_match_current_callers():
    holder, p = _patch_sdk_engine()
    with p:
        await LLMRouter().run_agent_task(task="t")
    k = holder["engine"].init_kwargs
    # Behaviour-preserving defaults: backend tools + MCP on, thinking off.
    assert k["use_backend_tools"] is True
    assert k["use_mcp_tools"] is True
    assert k["enable_thinking"] is False
    assert k["use_agent_sdk"] is True


@pytest.mark.asyncio
async def test_run_agent_task_passes_through_engine_error_dict():
    """Anthropic-only: when the SDK/engine can't run (e.g. no API key) the
    engine returns success=False; the router forwards it untouched — it does
    not raise or transform (behaviour-preserving)."""

    class _ErrEngine(_FakeSDKEngine):
        async def run_agent_task(self, task, agent_config=None, session_id=None):
            return {
                "task": task,
                "tool_calls": [],
                "final_result": "",
                "success": False,
                "error": "API key not configured",
            }

    with patch("services.claude_service.ClaudeService", lambda **k: _ErrEngine(**k)):
        out = await LLMRouter().run_agent_task(task="t")
    assert out["success"] is False
    assert out["error"] == "API key not configured"


@pytest.mark.asyncio
async def test_run_agent_task_mixed_config_keys_no_collision():
    """agent_config carries BOTH engine-construction hints and run params in a
    single dict: the hints must reach the constructor, AND the full dict (run
    params + hints) must still reach engine.run_agent_task untouched — no key
    is dropped or collides."""
    holder, p = _patch_sdk_engine()
    cfg = {
        "use_mcp_tools": False,
        "enable_thinking": True,
        "system_prompt": "sp",
        "model": "claude-z",
        "max_turns": 3,
    }
    with p:
        await LLMRouter().run_agent_task(task="t", agent_config=cfg, session_id="s")

    # Construction hints reached the constructor.
    assert holder["engine"].init_kwargs["use_mcp_tools"] is False
    assert holder["engine"].init_kwargs["enable_thinking"] is True
    # The full dict (hints + run params) reached run_agent_task verbatim.
    assert holder["engine"].task_calls == [("t", cfg, "s")]


@pytest.mark.asyncio
async def test_run_agent_stream_delegates_and_yields_events_verbatim():
    holder, p = _patch_sdk_engine()
    with p:
        events = [
            ev
            async for ev in LLMRouter().run_agent_stream(
                prompt="hunt",
                system_prompt="sp",
                allowed_tools=["get_case"],
                max_turns=7,
                session_id="s2",
                model="claude-y",
            )
        ]

    assert events == [
        {"type": "tool_use", "tool": "get_case"},
        {"type": "result", "content": "done"},
    ]
    qk = holder["engine"].query_kwargs
    assert qk["prompt"] == "hunt"
    assert qk["allowed_tools"] == ["get_case"]
    assert qk["max_turns"] == 7
    assert qk["session_id"] == "s2"
    assert qk["model"] == "claude-y"
    assert holder["engine"].init_kwargs["use_agent_sdk"] is True


@pytest.mark.asyncio
async def test_run_agent_stream_omits_model_when_not_given():
    """If no model is passed, the router must NOT override agent_query's own
    default with None."""
    holder, p = _patch_sdk_engine()
    with p:
        _ = [ev async for ev in LLMRouter().run_agent_stream(prompt="x")]
    # Fake's agent_query default is "unset"; router left it untouched.
    assert holder["engine"].query_kwargs["model"] == "unset"


# ---------------------------------------------------------------------------
# #413 PR4a — unified chat() text shim + dispatch_stream() entry point
# ---------------------------------------------------------------------------


class _FakeChatEngine:
    """Stand-in for ClaudeService on the chat/chat_stream delegation paths.

    Records constructor kwargs and the delegated call args so the shim tests
    can assert the Anthropic engine is driven faithfully.
    """

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.chat_calls = []
        self.stream_calls = []

    def chat(self, message, **kwargs):  # ClaudeService.chat is synchronous
        self.chat_calls.append((message, kwargs))
        return "anthropic-reply"

    async def chat_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        for ch in ({"type": "text", "content": "a"}, {"type": "text", "content": "b"}):
            yield ch


def _patch_chat_engine():
    holder: dict = {}

    def factory(**kwargs):
        engine = _FakeChatEngine(**kwargs)
        holder["engine"] = engine
        return engine

    return holder, patch("services.claude_service.ClaudeService", factory)


class _FakeOAS:
    """Stand-in for OpenAIAgentService on the tool-capable router stream path."""

    _tools_available = True

    def __init__(self, recommended_tools=None):
        self.recommended_tools = recommended_tools
        self.stream_kwargs = None

    def tools_available(self):
        return self._tools_available

    async def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        for ch in ({"type": "text", "content": "agent-hi"},):
            yield ch


def _patch_oas(tools_available=True):
    holder: dict = {}

    class _OAS(_FakeOAS):
        pass

    _OAS._tools_available = tools_available

    def factory(recommended_tools=None):
        inst = _OAS(recommended_tools=recommended_tools)
        holder["oas"] = inst
        return inst

    return holder, patch("services.openai_agent_service.OpenAIAgentService", factory)


def _patch_model_registry(supports_tools):
    reg = MagicMock()
    reg.get_model_info.return_value = SimpleNamespace(supports_tools=supports_tools)
    return patch("services.model_registry.ModelRegistry", return_value=reg)


# ---- chat() text shim (Contract A) ---------------------------------------


@pytest.mark.asyncio
async def test_chat_non_anthropic_dispatches_and_returns_text():
    router = LLMRouter()
    router.dispatch = AsyncMock(return_value={"content": "hi there"})

    out = await router.chat(
        "analyze this", provider=_ollama_spec(), model="claude-3-opus"
    )

    assert out == "hi there"
    _, kwargs = router.dispatch.call_args
    assert kwargs["provider"].provider_type == "ollama"
    # No-tools guardrail prompt used when caller gives none.
    assert kwargs["system_prompt"].startswith("You are Vigil, a concise SOC")
    # Stale claude-* selection pinned to the provider's own default model.
    assert kwargs["model"] == "llama3.1:8b"
    # Trailing user turn appended to context.
    assert kwargs["messages"][-1] == {"role": "user", "content": "analyze this"}


@pytest.mark.asyncio
async def test_chat_non_anthropic_preserves_explicit_system_prompt():
    router = LLMRouter()
    router.dispatch = AsyncMock(return_value={"content": "ok"})

    await router.chat("q", provider=_ollama_spec(), system_prompt="CUSTOM")

    _, kwargs = router.dispatch.call_args
    assert kwargs["system_prompt"] == "CUSTOM"


@pytest.mark.asyncio
async def test_chat_anthropic_delegates_to_engine_and_returns_str():
    holder, p = _patch_chat_engine()
    with p:
        out = await LLMRouter().chat(
            "hello",
            provider=_anthropic_spec(),
            system_prompt="sp",
            context=[{"role": "user", "content": "prev"}],
            recommended_tools=["get_case"],
        )

    assert out == "anthropic-reply"
    msg, kwargs = holder["engine"].chat_calls[0]
    assert msg == "hello"
    assert kwargs["system_prompt"] == "sp"
    assert kwargs["context"] == [{"role": "user", "content": "prev"}]
    assert kwargs["recommended_tools"] == ["get_case"]
    # model omitted (None) so ClaudeService's own default stands.
    assert "model" not in kwargs


@pytest.mark.asyncio
async def test_chat_resolves_default_provider_when_unset():
    router = LLMRouter()
    router.dispatch = AsyncMock(return_value={"content": "routed"})
    with patch(
        "services.llm_router.get_default_provider_spec", return_value=_ollama_spec()
    ):
        out = await router.chat("q")

    assert out == "routed"
    assert router.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_chat_forwards_service_config_to_engine_ctor():
    holder, p = _patch_chat_engine()
    with p:
        await LLMRouter().chat(
            "hi",
            provider=_anthropic_spec(),
            service_config={"use_backend_tools": True, "use_mcp_tools": False},
        )

    assert holder["engine"].init_kwargs["use_backend_tools"] is True
    assert holder["engine"].init_kwargs["use_mcp_tools"] is False


# ---- dispatch_stream() (Contract C) --------------------------------------


@pytest.mark.asyncio
async def test_dispatch_stream_anthropic_delegates_and_splits_history():
    holder, p = _patch_chat_engine()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "current"},
    ]
    with p:
        out = [
            c
            async for c in LLMRouter().dispatch_stream(
                provider=_anthropic_spec(),
                messages=messages,
                system_prompt="sp",
                enable_thinking=True,
                thinking_budget=5000,
            )
        ]

    assert out == [{"type": "text", "content": "a"}, {"type": "text", "content": "b"}]
    sk = holder["engine"].stream_calls[0]
    assert sk["message"] == "current"
    assert sk["context"] == messages[:-1]
    assert sk["enable_thinking"] is True


@pytest.mark.asyncio
async def test_dispatch_stream_no_provider_uses_anthropic_engine():
    holder, p = _patch_chat_engine()
    with p:
        out = [
            c
            async for c in LLMRouter().dispatch_stream(
                provider=None,
                messages=[{"role": "user", "content": "hi"}],
            )
        ]
    assert out == [{"type": "text", "content": "a"}, {"type": "text", "content": "b"}]
    assert holder["engine"].stream_calls[0]["context"] is None


@pytest.mark.asyncio
async def test_dispatch_stream_non_anthropic_no_tools_uses_openai_stream():
    router = LLMRouter()
    captured = {}

    async def fake_openai_stream(**kwargs):
        captured.update(kwargs)
        for ch in ({"type": "text", "content": "router-hi"},):
            yield ch

    router.dispatch_openai_stream = fake_openai_stream
    with _patch_model_registry(supports_tools=False):
        out = [
            c
            async for c in router.dispatch_stream(
                provider=_ollama_spec(),
                messages=[{"role": "user", "content": "q"}],
                model="claude-x",
            )
        ]

    assert out == [{"type": "text", "content": "router-hi"}]
    assert captured["system_prompt"].startswith("You are Vigil, a concise SOC")
    assert captured["model"] == "llama3.1:8b"  # _router_model pinned it


@pytest.mark.asyncio
async def test_dispatch_stream_tool_capable_uses_agent_with_guardrail():
    holder, p_oas = _patch_oas(tools_available=True)
    with _patch_model_registry(supports_tools=True), p_oas:
        out = [
            c
            async for c in LLMRouter().dispatch_stream(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "look it up"}],
                system_prompt="CALLER",
                recommended_tools=["get_case"],
            )
        ]

    assert out == [{"type": "text", "content": "agent-hi"}]
    sk = holder["oas"].stream_kwargs
    assert sk["enable_tools"] is True
    # Guardrail prepended to the caller's own prompt.
    assert sk["system_prompt"].startswith("You are Vigil, an AI-native SOC")
    assert sk["system_prompt"].endswith("CALLER")
    assert holder["oas"].recommended_tools == ["get_case"]


@pytest.mark.asyncio
async def test_dispatch_stream_tool_capable_but_none_loadable_falls_back():
    router = LLMRouter()
    captured = {}

    async def fake_openai_stream(**kwargs):
        captured.update(kwargs)
        for ch in ({"type": "text", "content": "fallback"},):
            yield ch

    router.dispatch_openai_stream = fake_openai_stream
    _, p_oas = _patch_oas(tools_available=False)
    with _patch_model_registry(supports_tools=True), p_oas:
        out = [
            c
            async for c in router.dispatch_stream(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "q"}],
            )
        ]

    # supports_tools=True but tools_available()=False → no-tools fallback.
    assert out == [{"type": "text", "content": "fallback"}]
    assert captured["system_prompt"].startswith("You are Vigil, a concise SOC")


@pytest.mark.asyncio
async def test_dispatch_stream_forwards_service_config_to_engine_ctor():
    holder, p = _patch_chat_engine()
    with p:
        _ = [
            c
            async for c in LLMRouter().dispatch_stream(
                provider=None,
                messages=[{"role": "user", "content": "hi"}],
                service_config={"use_mcp_tools": False},
            )
        ]
    assert holder["engine"].init_kwargs["use_backend_tools"] is True  # default
    assert holder["engine"].init_kwargs["use_mcp_tools"] is False  # override


# ---- 4a review follow-ups (test-gap closures) ----------------------------


@pytest.mark.asyncio
async def test_dispatch_stream_registry_error_falls_back_to_no_tools():
    """If the model-registry lookup raises, we must not crash — treat the
    model as tool-less and use the no-tools router stream."""
    router = LLMRouter()
    captured = {}

    async def fake_openai_stream(**kwargs):
        captured.update(kwargs)
        for ch in ({"type": "text", "content": "safe"},):
            yield ch

    router.dispatch_openai_stream = fake_openai_stream
    reg = MagicMock()
    reg.get_model_info.side_effect = RuntimeError("registry down")
    with patch("services.model_registry.ModelRegistry", return_value=reg):
        out = [
            c
            async for c in router.dispatch_stream(
                provider=_ollama_spec(),
                messages=[{"role": "user", "content": "q"}],
            )
        ]

    assert out == [{"type": "text", "content": "safe"}]
    assert captured["system_prompt"].startswith("You are Vigil, a concise SOC")


@pytest.mark.asyncio
async def test_chat_anthropic_forwards_explicit_model():
    holder, p = _patch_chat_engine()
    with p:
        await LLMRouter().chat(
            "hi", provider=_anthropic_spec(), model="claude-3-5-sonnet"
        )
    _, kwargs = holder["engine"].chat_calls[0]
    assert kwargs["model"] == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_chat_non_anthropic_assembles_multi_turn_context():
    router = LLMRouter()
    router.dispatch = AsyncMock(return_value={"content": "ok"})
    context = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    await router.chat("now", provider=_ollama_spec(), context=context)

    _, kwargs = router.dispatch.call_args
    assert kwargs["messages"] == context + [{"role": "user", "content": "now"}]


@pytest.mark.asyncio
async def test_dispatch_stream_service_config_overrides_use_backend_tools_false():
    holder, p = _patch_chat_engine()
    with p:
        _ = [
            c
            async for c in LLMRouter().dispatch_stream(
                provider=None,
                messages=[{"role": "user", "content": "hi"}],
                service_config={"use_backend_tools": False},
            )
        ]
    assert holder["engine"].init_kwargs["use_backend_tools"] is False


# ---- anthropic_api_key_available (#413 PR4c has_api_key replacement) ------


@pytest.mark.parametrize(
    "secrets, discover, expected",
    [
        ({"CLAUDE_API_KEY": "sk-x"}, None, True),
        ({"ANTHROPIC_API_KEY": "sk-y"}, None, True),
        # Lowercase legacy names ClaudeService._load_api_key also honours;
        # get_secret is case-sensitive so these are distinct lookups (review M1).
        ({"claude_api_key": "sk-lc"}, None, True),
        ({"anthropic_api_key": "sk-lc2"}, None, True),
        ({}, "sk-from-ui-row", True),  # env absent, UI row resolves
        ({}, None, False),  # nothing configured
    ],
)
def test_anthropic_api_key_available(secrets, discover, expected):
    from services.llm_router import anthropic_api_key_available

    with patch(
        "services.llm_router.get_secret", side_effect=lambda n: secrets.get(n)
    ), patch("services.llm_router.discover_anthropic_api_key", return_value=discover):
        assert anthropic_api_key_available() is expected


def test_anthropic_api_key_available_matches_claudeservice_name_set():
    """Parity guard: the helper must check the exact legacy name set
    ClaudeService._load_api_key uses (review M1 regression)."""
    from services.llm_router import anthropic_api_key_available

    checked: list = []

    def record(name):
        checked.append(name)
        return None

    with patch("services.llm_router.get_secret", side_effect=record), patch(
        "services.llm_router.discover_anthropic_api_key", return_value=None
    ):
        assert anthropic_api_key_available() is False
    assert checked == [
        "CLAUDE_API_KEY",
        "ANTHROPIC_API_KEY",
        "claude_api_key",
        "anthropic_api_key",
    ]


def test_anthropic_api_key_available_when_secrets_backend_absent():
    """get_secret is None (no secrets backend) → fall straight to discover."""
    from services.llm_router import anthropic_api_key_available

    with patch("services.llm_router.get_secret", None), patch(
        "services.llm_router.discover_anthropic_api_key", return_value="sk-ui"
    ):
        assert anthropic_api_key_available() is True


def test_anthropic_api_key_available_survives_get_secret_error():
    """A raising get_secret must not crash the gate — fall through to discover."""
    from services.llm_router import anthropic_api_key_available

    def boom(_name):
        raise RuntimeError("secrets backend down")

    with patch("services.llm_router.get_secret", side_effect=boom), patch(
        "services.llm_router.discover_anthropic_api_key", return_value="sk-ui"
    ):
        assert anthropic_api_key_available() is True


def test_agent_sdk_available_true_when_importable():
    from services.llm_router import agent_sdk_available

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        assert agent_sdk_available() is True


def test_agent_sdk_available_false_when_missing():
    import builtins

    from services.llm_router import agent_sdk_available

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "claude_agent_sdk":
            raise ImportError("no sdk")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        assert agent_sdk_available() is False


# --- run_agent_chat (#413 4d-2) --------------------------------------------


class _FakeOASRun:
    """OpenAIAgentService stand-in exposing the non-streaming run()."""

    def __init__(self, recommended_tools=None):
        self.recommended_tools = recommended_tools
        self.run_kwargs = None

    async def run(self, **kwargs):
        self.run_kwargs = kwargs
        return "oas-final-text"


def _patch_oas_run():
    holder: dict = {}

    def factory(recommended_tools=None):
        inst = _FakeOASRun(recommended_tools=recommended_tools)
        holder["oas"] = inst
        return inst

    return holder, patch("services.openai_agent_service.OpenAIAgentService", factory)


@pytest.mark.asyncio
async def test_run_agent_chat_anthropic_path_builds_service_and_calls_chat():
    holder, p = _patch_chat_engine()
    with p:
        out = await LLMRouter().run_agent_chat(
            "investigate F1",
            provider=None,
            model="claude-x",
            system_prompt="sys",
            recommended_tools=["list_findings"],
            max_tokens=8192,
            service_config={
                "use_backend_tools": True,
                "use_mcp_tools": True,
                "use_agent_sdk": False,
                "enable_thinking": True,
            },
        )
    assert out == "anthropic-reply"
    eng = holder["engine"]
    # service_config drives the ClaudeService construction verbatim.
    assert eng.init_kwargs == {
        "use_backend_tools": True,
        "use_mcp_tools": True,
        "use_agent_sdk": False,
        "enable_thinking": True,
    }
    # chat() gets the workflow turn args (no session_id/agent_id on this path,
    # matching the old _run_agent_turn Anthropic branch).
    msg, kwargs = eng.chat_calls[0]
    assert msg == "investigate F1"
    assert kwargs["system_prompt"] == "sys"
    assert kwargs["model"] == "claude-x"
    assert kwargs["max_tokens"] == 8192
    assert kwargs["recommended_tools"] == ["list_findings"]


@pytest.mark.asyncio
async def test_run_agent_chat_non_anthropic_uses_openai_agent_run():
    holder, p = _patch_oas_run()
    with p:
        out = await LLMRouter().run_agent_chat(
            "investigate F1",
            provider=_ollama_spec(),
            model="llama3",
            system_prompt="sys",
            recommended_tools=["list_findings"],
            max_tokens=4096,
            session_id="run-1",
            agent_id="triage",
        )
    assert out == "oas-final-text"
    oas = holder["oas"]
    assert oas.recommended_tools == ["list_findings"]
    k = oas.run_kwargs
    assert getattr(k["provider"], "provider_type", None) == "ollama"
    assert k["messages"] == [{"role": "user", "content": "investigate F1"}]
    assert k["enable_tools"] is True
    assert k["session_id"] == "run-1"
    assert k["agent_id"] == "triage"
    assert k["model"] == "llama3"


@pytest.mark.asyncio
async def test_run_agent_chat_none_provider_does_not_resolve_default(monkeypatch):
    # Unlike chat(), provider=None must NOT resolve the default provider — it
    # deterministically selects the Anthropic path. Guard against a regression
    # that adds default resolution here.
    called = {"resolved": False}

    def _sentinel():
        called["resolved"] = True
        return _ollama_spec()

    monkeypatch.setattr("services.llm_router.get_default_provider_spec", _sentinel)
    holder, p = _patch_chat_engine()
    with p:
        out = await LLMRouter().run_agent_chat("x", provider=None)
    assert out == "anthropic-reply"
    assert called["resolved"] is False
