"""Unit tests for the OpenAI-format agent loop (services/openai_agent_service.py).

Covers the schema converter, loop-detection signature canonicalization, and the
streaming tool loop (tool-call, error flag, malformed tool call) with a mocked
AsyncOpenAI client — no network or DB required.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.openai_agent_service import (  # noqa: E402
    OpenAIAgentService,
    anthropic_tools_to_openai,
    _canonical_args,
    _LOOP_DETECT_THRESHOLD,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
class TestSchemaConverter:
    def test_basic_conversion(self):
        out = anthropic_tools_to_openai(
            [{"name": "search", "description": "d", "input_schema": {"type": "object"}}]
        )
        assert out[0] == {
            "type": "function",
            "function": {
                "name": "search",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }

    def test_missing_input_schema_falls_back(self):
        out = anthropic_tools_to_openai([{"name": "x"}])
        assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}
        assert out[0]["function"]["description"] == ""

    def test_empty_list(self):
        assert anthropic_tools_to_openai([]) == []


class TestCanonicalArgs:
    def test_key_order_and_whitespace_equivalent(self):
        assert _canonical_args('{"b": 1, "a": 2}') == _canonical_args('{"a":2,"b":1}')

    def test_invalid_json_falls_back_to_stripped_raw(self):
        assert _canonical_args("  not json  ") == "not json"


class TestLoopDetection:
    def test_below_threshold_not_a_loop(self):
        assert OpenAIAgentService._detect_infinite_loop(deque(["a"], maxlen=5)) is False

    def test_identical_repeats_is_a_loop(self):
        hist = deque(["sig"] * _LOOP_DETECT_THRESHOLD, maxlen=5)
        assert OpenAIAgentService._detect_infinite_loop(hist) is True

    def test_alternating_calls_not_a_loop(self):
        hist = deque(["a", "b", "a"], maxlen=5)
        assert OpenAIAgentService._detect_infinite_loop(hist) is False


class TestToolFiltering:
    def _svc(self, recommended):
        return OpenAIAgentService(
            backend_tools=[{"name": "splunk_search", "input_schema": {}}],
            include_mcp_tools=False,
            recommended_tools=recommended,
        )

    def test_no_recommended_keeps_all(self):
        assert self._svc(None).tools_available() is True

    def test_recommended_server_tool_suffix_match(self):
        # "splunk_search" should match a recommendation of bare "search"
        assert self._svc(["search"]).tools_available() is True

    def test_recommended_no_match_hides_tools(self):
        assert self._svc(["nonexistent"]).tools_available() is False


# --------------------------------------------------------------------------
# Streaming loop with a mocked AsyncOpenAI client
# --------------------------------------------------------------------------
def _chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    )


def _tc(index, tc_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeStream:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    async def create(self, **_kwargs):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return _FakeStream(resp)


def _fake_openai_factory(responses):
    def _factory(**_kwargs):
        return SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(responses)),
            close=AsyncMock(),
        )

    return _factory


def _agent():
    return OpenAIAgentService(
        backend_tools=[{"name": "mytool", "input_schema": {"type": "object"}}],
        include_mcp_tools=False,
    )


async def _collect(agent, responses, provider):
    events = []
    with patch("openai.AsyncOpenAI", _fake_openai_factory(responses)), patch(
        "services.openai_agent_service.LLMRouter",
        return_value=SimpleNamespace(bifrost_url="http://bifrost:8080"),
    ):
        async for ev in agent.stream(
            provider=provider, messages=[{"role": "user", "content": "hi"}]
        ):
            events.append(ev)
    return events


@pytest.fixture()
def provider():
    return SimpleNamespace(provider_type="ollama", default_model="llama3.1:8b")


@pytest.mark.asyncio
async def test_text_only_response_streams_text_and_stops(provider):
    responses = [
        [_chunk(content="hello "), _chunk(content="world", finish_reason="stop")]
    ]
    events = await _collect(_agent(), responses, provider)
    texts = [e["content"] for e in events if e["type"] == "text"]
    assert "".join(texts) == "hello world"
    assert not any(e["type"] == "tool_processing" for e in events)


@pytest.mark.asyncio
async def test_tool_call_executes_then_finishes(provider):
    responses = [
        [
            _chunk(
                tool_calls=[_tc(0, "call1", "mytool", '{"q":"x"}')],
                finish_reason="tool_calls",
            )
        ],
        [_chunk(content="done", finish_reason="stop")],
    ]
    agent = _agent()
    agent._execute_tool = AsyncMock(return_value=("RESULT", False))
    events = await _collect(agent, responses, provider)

    proc = [e for e in events if e["type"] == "tool_processing"]
    res = [e for e in events if e["type"] == "tool_result"]
    assert proc and proc[0]["tool_name"] == "mytool"
    assert res and res[0]["is_error"] is False and res[0]["result"].startswith("RESULT")
    agent._execute_tool.assert_awaited_once()
    assert any(e["type"] == "text" and e["content"] == "done" for e in events)


@pytest.mark.asyncio
async def test_tool_error_flag_propagates(provider):
    responses = [
        [
            _chunk(
                tool_calls=[_tc(0, "call1", "mytool", "{}")], finish_reason="tool_calls"
            )
        ],
        [_chunk(content="ok", finish_reason="stop")],
    ]
    agent = _agent()
    agent._execute_tool = AsyncMock(return_value=("boom", True))
    events = await _collect(agent, responses, provider)
    res = [e for e in events if e["type"] == "tool_result"]
    assert res and res[0]["is_error"] is True


@pytest.mark.asyncio
async def test_empty_tool_name_is_skipped_no_crash(provider):
    # A tool-call slot that never receives a name must be skipped, not emitted.
    responses = [
        [_chunk(tool_calls=[_tc(0, "call1", None, "{}")], finish_reason="tool_calls")]
    ]
    agent = _agent()
    agent._execute_tool = AsyncMock(return_value=("x", False))
    events = await _collect(agent, responses, provider)
    assert not any(e["type"] == "tool_processing" for e in events)
    agent._execute_tool.assert_not_awaited()
