"""Unit tests for core.llm.router.router (GH #88).

Exercises the pure-logic path-selection rules and the dispatch wiring
with mocked openai / anthropic clients.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.llm.router.router import (LLMRouter, ProviderSpec,
                                    provider_spec_from_row)

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
    assert kwargs["reasoning_effort"] == "none"

    assert out["path"] == "bifrost"
    assert out["provider"] == "ollama"
    assert out["content"] == "hello"
    assert out["input_tokens"] == 5
    assert out["output_tokens"] == 7
    # The OpenAI-format dispatcher must close its client (no httpx pool leak).
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_bifrost_maps_enabled_ollama_thinking_to_reasoning_effort():
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))
        ],
        model="ollama/llama3.1:8b",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "think"}],
            enable_thinking=True,
        )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["reasoning_effort"] == "medium"


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
async def test_dispatch_bifrost_records_genai_metrics_once():
    """#894: one successful dispatch records exactly once, with the returned
    usage and the priced cost (attribute shape is covered in test_telemetry)."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="openai/gpt-4o-mini",
        usage=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=200,
            prompt_tokens_details=SimpleNamespace(cached_tokens=750),
        ),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client), patch(
        "core.llm.router.router.record_llm_call"
    ) as record, patch(
        "core.llm.router.router.compute_call_cost", return_value=0.0123
    ) as cost:
        await router.dispatch(
            provider=_openai_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )

    cost.assert_called_once_with(
        "gpt-4o-mini",
        "openai",
        1000,
        200,
        cache_read_tokens=750,
        cache_creation_tokens=0,
    )
    record.assert_called_once()
    kw = record.call_args.kwargs
    assert kw["model"] == "gpt-4o-mini"  # requested id, not Bifrost's echo
    assert kw["provider"] == "openai"
    assert (kw["input_tokens"], kw["output_tokens"], kw["cache_read_tokens"]) == (
        1000,
        200,
        750,
    )
    assert kw["cost_usd"] == 0.0123
    assert kw["duration_s"] >= 0


@pytest.mark.asyncio
async def test_dispatch_bifrost_survives_metrics_failure():
    """#894: telemetry must never raise on the dispatch path."""
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        model="ollama/llama3.1:8b",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_client), patch(
        "core.llm.router.router.compute_call_cost", side_effect=RuntimeError("boom")
    ):
        out = await router.dispatch(
            provider=_ollama_spec(),
            messages=[{"role": "user", "content": "hi"}],
        )
    assert out["content"] == "ok"


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
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in kwargs
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
        "core.llm.cost.budget.should_enforce", return_value=True
    ), patch("core.llm.cost.budget.get_active_vk", return_value="sk-bf-test-vk"):
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
        "core.llm.cost.budget.should_enforce", return_value=False
    ), patch("core.llm.cost.budget.get_active_vk", return_value="sk-bf-test-vk"):
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
    from core.llm.cost.budget import BudgetExceeded

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
        "core.llm.cost.budget.should_enforce", return_value=True
    ), patch("core.llm.cost.budget.get_active_vk", return_value="sk-bf-test"):
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
async def test_dispatch_retries_a_429_rather_than_failing_the_run():
    """A rate limit is not a budget failure.

    This asserted the opposite until the two halves were unified: 429 raised
    BudgetExceeded(tier="rate_limit") and nothing caught it, so a two-second
    wait failed the run and was reported as being out of credit. The agent
    worker had always retried the same response.
    """
    router = LLMRouter(bifrost_url="http://test-bifrost:8080")
    limited = type("FakeAPIErr", (Exception,), {})("rate limited")
    limited.status_code = 429  # type: ignore[attr-defined]

    answered = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="hello", tool_calls=None))
        ],
        model="gpt-4o",
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=[limited, answered])

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        with patch("core.llm.gateway_retry.asyncio.sleep", new=AsyncMock()):
            out = await router.dispatch(
                provider=_openai_spec(),
                messages=[{"role": "user", "content": "hi"}],
            )

    assert out["content"] == "hello"
    assert mock_client.chat.completions.create.await_count == 2


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
    from core.llm.cost.budget import BudgetExceeded

    assert not isinstance(excinfo.value, BudgetExceeded)


# ---------------------------------------------------------------------------
# provider_spec_from_row
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Non-default Anthropic providers must route through the router so the
# per-provider api_key_ref is resolved (regression for PR #103 review).
# ---------------------------------------------------------------------------


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
    from core.llm.router import router as llm_router

    default_row = SimpleNamespace(
        provider_id="anthropic-default",
        api_key_ref="llm_provider_anthropic-default_api_key",
    )
    session = _stub_session([default_row])

    with patch.object(llm_router, "get_secret", return_value="sk-ant-ui-saved"), patch(
        "core.storage.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() == "sk-ant-ui-saved"


def test_discover_anthropic_api_key_falls_through_to_active_row():
    """If the default row's secret is missing, the next active row wins."""
    from core.llm.router import router as llm_router

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
        "core.storage.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() == "sk-ant-team-key"


def test_discover_anthropic_api_key_returns_none_when_no_rows():
    from core.llm.router import router as llm_router

    session = _stub_session([])
    with patch.object(llm_router, "get_secret", return_value=None), patch(
        "core.storage.connection.get_db_session", return_value=session
    ):
        assert llm_router.discover_anthropic_api_key() is None


def test_discover_anthropic_api_key_returns_none_when_db_unavailable():
    """DB import error => silent None, so the legacy chain stays usable
    in environments where core.storage.connection can't import."""
    # Patch ``get_db_session`` to raise on import. Easiest: make the
    # entire ``core.storage.connection`` import fail by patching builtins.
    import builtins

    from core.llm.router import router as llm_router

    real_import = builtins.__import__

    def boom_import(name, *args, **kwargs):
        if name == "core.storage.connection":
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
    assert kwargs["reasoning_effort"] == "none"
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
                enable_thinking=True,
            )
        ]

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["reasoning_effort"] == "medium"
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


# Dropped with the second schema (#644). Every one asserted something about
# choosing between two egress paths, and there is one:
#
#   test_path_* and test_router_class_method_matches_free_function — select_path
#     returned the constant "bifrost" for every provider before it was deleted.
#     What it was really pinning is now a ratchet:
#     tests/unit/_ratchets/test_one_egress.py.
#
#   test_dispatch_anthropic_*, test_anthropic_dispatch_raises_when_no_key,
#   test_dispatch_propagates_interaction_id_anthropic — _dispatch_anthropic took
#     Bifrost's /anthropic passthrough to keep extended thinking and
#     cache_control. ADR 0011 traded both away and by #632 nothing requested
#     either, so the passthrough preserved features with no caller. The OpenAI
#     equivalents of these cases are kept above.
#
#   test_*_anthropic_with_thinking_* and test_is_default_anthropic_recognizes_
#   legacy_refs — the fallback they described handed default-Anthropic thinking
#     calls to ClaudeService for its tool loop. That loop went in #631.
