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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.openai_agent_service import (  # noqa: E402
    _LOOP_DETECT_THRESHOLD,
    OpenAIAgentService,
    PendingApprovalError,
    _canonical_args,
    anthropic_tools_to_openai,
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
    # The router builds (and closes) a client per iteration, so the response
    # queue is shared across constructions to let the script advance.
    completions = _FakeCompletions(responses)

    def _factory(**_kwargs):
        return SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=AsyncMock(),
        )

    return _factory


def _agent():
    return OpenAIAgentService(
        backend_tools=[{"name": "mytool", "input_schema": {"type": "object"}}],
        include_mcp_tools=False,
    )


async def _collect(agent, responses, provider):
    """Drive agent.stream with a faked SDK client; dispatch runs through the
    real LLMRouter so sanitize/header/conversion wiring is covered too."""
    events = []
    with patch("openai.AsyncOpenAI", _fake_openai_factory(responses)):
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


# --------------------------------------------------------------------------
# Tool safety gating (parity with the daemon tier policy)
# --------------------------------------------------------------------------
class TestToolGating:
    @pytest.mark.asyncio
    async def test_forbidden_tool_is_refused_not_executed(self):
        agent = OpenAIAgentService(include_mcp_tools=False)
        agent._execute_backend_tool = AsyncMock()
        agent._execute_mcp_tool = AsyncMock()
        result, is_error = await agent._execute_tool("delete_case", "{}")
        assert is_error is True
        assert "forbidden" in result.lower()
        agent._execute_backend_tool.assert_not_awaited()
        agent._execute_mcp_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requires_approval_creates_action_and_does_not_execute(self):
        agent = OpenAIAgentService(include_mcp_tools=False)
        agent._execute_backend_tool = AsyncMock()
        agent._execute_mcp_tool = AsyncMock()

        created = {}

        def _create_action(**kwargs):
            created.update(kwargs)
            return SimpleNamespace(action_id="ACT-1")

        fake_service = SimpleNamespace(create_action=_create_action)
        with patch(
            "services.approval_service.get_approval_service",
            return_value=fake_service,
        ), patch("services.approval_service.ActionType", side_effect=ValueError):
            with pytest.raises(PendingApprovalError) as exc_info:
                await agent._execute_tool("isolate_host", '{"host": "h1"}')

        result = str(exc_info.value)
        assert "ACT-1" in result and "approval" in result.lower()
        assert exc_info.value.action_id == "ACT-1"
        assert created["parameters"] == {"host": "h1"}
        agent._execute_backend_tool.assert_not_awaited()
        agent._execute_mcp_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approved_flag_bypasses_gate_and_executes(self):
        agent = OpenAIAgentService(
            backend_tools=[{"name": "isolate_host", "input_schema": {}}],
            include_mcp_tools=False,
        )
        agent._execute_backend_tool = AsyncMock(return_value=("isolated", False))
        result, is_error = await agent._execute_tool(
            "isolate_host", '{"host": "h1"}', approved=True
        )
        assert result == "isolated" and is_error is False
        agent._execute_backend_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approved_flag_does_not_bypass_forbidden(self):
        agent = OpenAIAgentService(include_mcp_tools=False)
        agent._execute_backend_tool = AsyncMock()
        agent._execute_mcp_tool = AsyncMock()
        result, is_error = await agent._execute_tool("delete_case", "{}", approved=True)
        assert is_error is True and "forbidden" in result.lower()
        agent._execute_backend_tool.assert_not_awaited()
        agent._execute_mcp_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_tool_still_executes(self):
        agent = OpenAIAgentService(
            backend_tools=[{"name": "list_findings", "input_schema": {}}],
            include_mcp_tools=False,
        )
        agent._execute_backend_tool = AsyncMock(return_value=("ok", False))
        result, is_error = await agent._execute_tool("list_findings", "{}")
        assert result == "ok" and is_error is False
        agent._execute_backend_tool.assert_awaited_once()


# --------------------------------------------------------------------------
# Approval gate — the run must not proceed past a requires_approval tool
# until an operator decides (mirrors the daemon's waiting_approval halt).
# --------------------------------------------------------------------------
def _action(status, rejection_reason=None):
    return SimpleNamespace(
        action_id="ACT-1", status=status, rejection_reason=rejection_reason
    )


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_approved_action_executes_then_run_continues(self, provider):
        responses = [
            [
                _chunk(
                    tool_calls=[_tc(0, "call1", "isolate_host", '{"host":"h1"}')],
                    finish_reason="tool_calls",
                )
            ],
            [_chunk(content="contained", finish_reason="stop")],
        ]
        agent = _agent()
        agent._await_approval = AsyncMock(return_value=("approved", "approved", 0.0))
        agent._mark_action_executed = MagicMock()
        agent._execute_tool = AsyncMock(
            side_effect=[
                PendingApprovalError("needs approval", "ACT-1"),
                ("isolated", False),
            ]
        )
        events = await _collect(agent, responses, provider)

        assert [e["action_id"] for e in events if e["type"] == "approval_required"] == [
            "ACT-1"
        ]
        res = [e for e in events if e["type"] == "tool_result"]
        assert res and res[0]["result"].startswith("isolated")
        # Re-run with approved=True, and the run carried on to the next turn.
        assert agent._execute_tool.await_args_list[-1].kwargs == {"approved": True}
        agent._mark_action_executed.assert_called_once()
        assert any(e["type"] == "text" and e["content"] == "contained" for e in events)

    @pytest.mark.asyncio
    async def test_rejected_action_is_not_executed_and_model_is_told(self, provider):
        responses = [
            [
                _chunk(
                    tool_calls=[_tc(0, "call1", "isolate_host", '{"host":"h1"}')],
                    finish_reason="tool_calls",
                )
            ],
            [_chunk(content="understood", finish_reason="stop")],
        ]
        agent = _agent()
        agent._await_approval = AsyncMock(return_value=("rejected", "too risky", 0.0))
        agent._execute_tool = AsyncMock(
            side_effect=PendingApprovalError("needs approval", "ACT-1")
        )
        events = await _collect(agent, responses, provider)

        res = [e for e in events if e["type"] == "tool_result"]
        assert res and res[0]["is_error"] is True
        assert "REJECTED" in res[0]["result"] and "too risky" in res[0]["result"]
        # Only the gated attempt ran — never a second, approved execution.
        agent._execute_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_halts_the_run_with_an_error(self, provider):
        responses = [
            [
                _chunk(
                    tool_calls=[_tc(0, "call1", "isolate_host", '{"host":"h1"}')],
                    finish_reason="tool_calls",
                )
            ],
            [_chunk(content="should never be reached", finish_reason="stop")],
        ]
        agent = _agent()
        agent._await_approval = AsyncMock(return_value=("timeout", "timed out", 0.0))
        agent._execute_tool = AsyncMock(
            side_effect=PendingApprovalError("needs approval", "ACT-1")
        )
        events = await _collect(agent, responses, provider)

        assert events[-1] == {"type": "error", "content": "timed out"}
        assert not any(
            e["type"] == "text" and "should never" in e.get("content", "")
            for e in events
        )

    @pytest.mark.asyncio
    async def test_uncreatable_approval_fails_closed(self, provider):
        responses = [
            [
                _chunk(
                    tool_calls=[_tc(0, "call1", "isolate_host", '{"host":"h1"}')],
                    finish_reason="tool_calls",
                )
            ],
            [_chunk(content="should never be reached", finish_reason="stop")],
        ]
        agent = _agent()
        agent._await_approval = AsyncMock()
        agent._execute_tool = AsyncMock(
            side_effect=PendingApprovalError("queue is down", None)
        )
        events = await _collect(agent, responses, provider)

        assert events[-1]["type"] == "error"
        # Nothing to poll, so we must not wait — just stop.
        agent._await_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_await_approval_polls_until_operator_decides(self):
        agent = _agent()
        fake_service = SimpleNamespace(
            get_action=MagicMock(
                side_effect=[
                    _action("pending"),
                    _action("pending"),
                    _action("approved"),
                ]
            )
        )
        with patch(
            "services.approval_service.get_approval_service", return_value=fake_service
        ), patch("services.openai_agent_service._APPROVAL_POLL_INTERVAL_S", 0.0):
            decision, _detail, _waited = await agent._await_approval("ACT-1")
        assert decision == "approved"
        assert fake_service.get_action.call_count == 3

    @pytest.mark.asyncio
    async def test_await_approval_treats_vanished_action_as_rejected(self):
        agent = _agent()
        fake_service = SimpleNamespace(get_action=MagicMock(return_value=None))
        with patch(
            "services.approval_service.get_approval_service", return_value=fake_service
        ):
            decision, _detail, _waited = await agent._await_approval("ACT-1")
        assert decision == "rejected"

    @pytest.mark.asyncio
    async def test_await_approval_times_out_when_nobody_decides(self):
        agent = _agent()
        fake_service = SimpleNamespace(
            get_action=MagicMock(return_value=_action("pending"))
        )
        with patch(
            "services.approval_service.get_approval_service", return_value=fake_service
        ), patch("services.openai_agent_service._APPROVAL_POLL_INTERVAL_S", 0.0):
            decision, detail, _waited = await agent._await_approval(
                "ACT-1", timeout=0.0
            )
        assert decision == "timeout" and "ACT-1" in detail
