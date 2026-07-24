"""Unit tests for the provider-agnostic agent loop (services/agent_loop.py).

These exercise the two halves of the refactored OpenAI agent loop in isolation:

  - ``LoopController`` — the provider-agnostic loop skeleton (iteration/wall-clock
    guards, backoff, loop-detection, tool exec + approval flow, telemetry, the
    iteration-limit message). Driven here against a *fake* engine and a *fake*
    runtime so the control flow is tested without any provider or DB.
  - ``OpenAITurnEngine`` — the OpenAI dialect (delta reassembly, tool-call
    normalization, usage, the malformed-tool-JSON heuristic). Driven with a
    mocked ``stream_openai_raw``.

The end-to-end behaviour parity guard lives in test_openai_agent_service.py; this
file adds focused coverage of the new seam.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.agent_loop import (LoopController,  # noqa: E402
                                 NormalizedToolCall, OpenAITurnEngine,
                                 PendingApprovalError, TurnResult,
                                 _canonical_args, _inter_iteration_delay)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
class TestCanonicalArgs:
    def test_key_order_and_whitespace_equivalent(self):
        assert _canonical_args('{"b": 1, "a": 2}') == _canonical_args('{"a":2,"b":1}')

    def test_invalid_json_falls_back_to_stripped_raw(self):
        assert _canonical_args("  not json  ") == "not json"


class TestBackoff:
    def test_flat_before_threshold(self):
        assert _inter_iteration_delay(1) == 0.5
        assert _inter_iteration_delay(15) == 0.5

    def test_ramps_and_caps_after_threshold(self):
        assert _inter_iteration_delay(16) > 0.5
        assert _inter_iteration_delay(100) == 3.0


# --------------------------------------------------------------------------
# LoopController — driven by a fake engine + fake runtime
# --------------------------------------------------------------------------
class _FakeEngine:
    """Scripts a sequence of TurnResults; records history mutations."""

    provider_type = "ollama"
    model = "llama3.1:8b"
    model_label = "ollama/llama3.1:8b"

    def __init__(self, turns, *, deltas_per_turn=None):
        # turns: list of TurnResult; deltas_per_turn: optional list-of-lists of
        # SSE delta dicts the engine yields before each turn's terminal result.
        # When omitted, a text delta is auto-emitted for each turn's text — as a
        # real engine streams text before yielding its terminal TurnResult (the
        # controller never re-emits turn.text itself).
        self._turns = list(turns)
        if deltas_per_turn is None:
            deltas_per_turn = [
                [{"type": "text", "content": t.text}] if t.text else [] for t in turns
            ]
        self._deltas = deltas_per_turn
        self._i = 0
        self.appended_assistant = []
        self.appended_tool_results = []

    async def stream_turn(self, *, iteration):
        idx = min(self._i, len(self._turns) - 1)
        for delta in self._deltas[idx] if idx < len(self._deltas) else []:
            yield delta
        turn = self._turns[idx]
        self._i += 1
        yield turn

    def append_assistant(self, turn):
        self.appended_assistant.append(turn)

    def append_tool_result(self, tool_call_id, result_text, is_error):
        self.appended_tool_results.append((tool_call_id, result_text, is_error))


def _runtime(**overrides):
    rt = SimpleNamespace(
        _execute_tool=AsyncMock(return_value=("RESULT", False)),
        _await_approval=AsyncMock(return_value=("approved", "approved", 0.0)),
        _mark_action_executed=MagicMock(),
        _compute_cost=MagicMock(return_value=0.0),
        _log_interaction=MagicMock(),
    )
    for k, v in overrides.items():
        setattr(rt, k, v)
    return rt


def _turn(*, text="", tool_calls=None, finish_reason="stop", raw_count=None):
    tcs = tool_calls or []
    assistant = {"role": "assistant"}
    if text:
        assistant["content"] = text
    if tcs:
        assistant["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in tcs
        ]
    return TurnResult(
        text=text,
        finish_reason=finish_reason,
        raw_tool_call_count=raw_count if raw_count is not None else len(tcs),
        tool_calls=tcs,
        assistant_message=assistant,
        malformed_help=None,
        interaction_id="iid",
        input_tokens=1,
        output_tokens=2,
        duration_ms=3,
    )


async def _drive(engine, runtime, **kwargs):
    controller = LoopController(engine=engine, runtime=runtime, **kwargs)
    return [ev async for ev in controller.run()]


@pytest.mark.asyncio
async def test_text_only_turn_stops_without_tools():
    engine = _FakeEngine(
        [_turn(text="hi", finish_reason="stop")],
        deltas_per_turn=[[{"type": "text", "content": "hi"}]],
    )
    runtime = _runtime()
    events = await _drive(engine, runtime)
    assert [e for e in events if e["type"] == "text"] == [
        {"type": "text", "content": "hi"}
    ]
    assert not any(e["type"] == "tool_processing" for e in events)
    runtime._execute_tool.assert_not_awaited()
    runtime._log_interaction.assert_called_once()


@pytest.mark.asyncio
async def test_tool_call_executes_then_finishes():
    tc = NormalizedToolCall(id="call1", name="mytool", arguments='{"q":"x"}')
    engine = _FakeEngine(
        [
            _turn(tool_calls=[tc], finish_reason="tool_calls"),
            _turn(text="done", finish_reason="stop"),
        ]
    )
    runtime = _runtime()
    events = await _drive(engine, runtime)
    assert [
        e["type"] for e in events if e["type"] in ("tool_processing", "tool_result")
    ] == [
        "tool_processing",
        "tool_result",
    ]
    runtime._execute_tool.assert_awaited_once_with("mytool", '{"q":"x"}')
    assert engine.appended_tool_results == [("call1", "RESULT", False)]
    assert any(e.get("content") == "done" for e in events)


@pytest.mark.asyncio
async def test_tool_error_flag_propagates():
    tc = NormalizedToolCall(id="c", name="mytool", arguments="{}")
    engine = _FakeEngine(
        [
            _turn(tool_calls=[tc], finish_reason="tool_calls"),
            _turn(text="ok", finish_reason="stop"),
        ]
    )
    runtime = _runtime(_execute_tool=AsyncMock(return_value=("boom", True)))
    events = await _drive(engine, runtime)
    res = [e for e in events if e["type"] == "tool_result"]
    assert res and res[0]["is_error"] is True
    assert engine.appended_tool_results == [("c", "boom", True)]


@pytest.mark.asyncio
async def test_loop_detection_halts_on_repeats():
    tc = NormalizedToolCall(id="c", name="mytool", arguments='{"q":"x"}')
    # Same tool call every turn -> should trip the infinite-loop guard.
    engine = _FakeEngine(
        [_turn(tool_calls=[tc], finish_reason="tool_calls") for _ in range(6)]
    )
    runtime = _runtime()
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine, runtime)
    assert any(e["type"] == "text" and "infinite loop" in e["content"] for e in events)


@pytest.mark.asyncio
async def test_iteration_limit_message():
    # Distinct args each turn so loop-detection never trips; hit the cap instead.
    turns = [
        _turn(
            tool_calls=[
                NormalizedToolCall(id=f"c{i}", name="t", arguments=f'{{"i":{i}}}')
            ],
            finish_reason="tool_calls",
        )
        for i in range(10)
    ]
    engine = _FakeEngine(turns)
    runtime = _runtime()
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine, runtime, max_iterations=3)
    assert events[-1]["type"] == "text"
    assert "iteration limit" in events[-1]["content"].lower()


@pytest.mark.asyncio
async def test_wall_clock_guard_stops_immediately():
    engine = _FakeEngine([_turn(text="never", finish_reason="stop")])
    runtime = _runtime()
    # Negative budget => the very first elapsed check trips.
    events = await _drive(engine, runtime, max_processing_time_s=-1.0)
    assert events[-1]["type"] == "text"
    assert "maximum processing time" in events[-1]["content"].lower()
    runtime._execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_error_is_surfaced_and_stops():
    class _BoomEngine(_FakeEngine):
        async def stream_turn(self, *, iteration):
            yield {"type": "text", "content": "partial"}
            raise RuntimeError("provider exploded")

    engine = _BoomEngine([_turn()])
    runtime = _runtime()
    events = await _drive(engine, runtime)
    assert events[-1] == {"type": "error", "content": "provider exploded"}
    assert {"type": "text", "content": "partial"} in events
    runtime._log_interaction.assert_not_called()


# --------------------------------------------------------------------------
# Approval flow through the controller
# --------------------------------------------------------------------------
class TestApprovalFlow:
    def _approval_engine(self):
        tc = NormalizedToolCall(
            id="call1", name="isolate_host", arguments='{"host":"h1"}'
        )
        return _FakeEngine(
            [
                _turn(tool_calls=[tc], finish_reason="tool_calls"),
                _turn(text="contained", finish_reason="stop"),
            ]
        )

    @pytest.mark.asyncio
    async def test_approved_reexecutes_and_continues(self):
        engine = self._approval_engine()
        runtime = _runtime(
            _execute_tool=AsyncMock(
                side_effect=[
                    PendingApprovalError("needs approval", "ACT-1"),
                    ("isolated", False),
                ]
            ),
            _await_approval=AsyncMock(return_value=("approved", "approved", 0.0)),
        )
        with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
            events = await _drive(engine, runtime)
        assert [e["action_id"] for e in events if e["type"] == "approval_required"] == [
            "ACT-1"
        ]
        assert runtime._execute_tool.await_args_list[-1].kwargs == {"approved": True}
        runtime._mark_action_executed.assert_called_once()
        assert any(e.get("content") == "contained" for e in events)

    @pytest.mark.asyncio
    async def test_rejected_is_reported_not_executed(self):
        engine = self._approval_engine()
        runtime = _runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("needs approval", "ACT-1")
            ),
            _await_approval=AsyncMock(return_value=("rejected", "too risky", 0.0)),
        )
        with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
            events = await _drive(engine, runtime)
        res = [e for e in events if e["type"] == "tool_result"]
        assert res and res[0]["is_error"] is True
        assert "REJECTED" in res[0]["result"] and "too risky" in res[0]["result"]
        runtime._execute_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timeout_halts_run(self):
        engine = self._approval_engine()
        runtime = _runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("needs approval", "ACT-1")
            ),
            _await_approval=AsyncMock(return_value=("timeout", "timed out", 0.0)),
        )
        with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
            events = await _drive(engine, runtime)
        assert events[-1] == {"type": "error", "content": "timed out"}
        assert not any(e.get("content") == "contained" for e in events)

    @pytest.mark.asyncio
    async def test_uncreatable_approval_fails_closed(self):
        engine = self._approval_engine()
        await_mock = AsyncMock()
        runtime = _runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("queue down", None)
            ),
            _await_approval=await_mock,
        )
        with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
            events = await _drive(engine, runtime)
        assert events[-1]["type"] == "error"
        await_mock.assert_not_awaited()


# --------------------------------------------------------------------------
# OpenAITurnEngine — delta reassembly against a mocked stream_openai_raw
# --------------------------------------------------------------------------
def _usage_chunk(prompt, completion):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return SimpleNamespace(choices=[], usage=usage)


def _tc(index, tc_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=tc_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


class _StreamChunk:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
        self.usage = None


def _engine(tools=None, iteration_messages=None):
    return OpenAITurnEngine(
        provider=SimpleNamespace(provider_type="ollama", default_model="llama3.1:8b"),
        messages=iteration_messages or [{"role": "user", "content": "hi"}],
        system_prompt=None,
        model="llama3.1:8b",
        max_tokens=256,
        temperature=None,
        tools=tools or [],
        history_window=20,
    )


async def _run_turn(engine, chunks, iteration=0):
    async def _fake_stream(**_kwargs):
        for c in chunks:
            yield c

    events = []
    result = None
    with patch(
        "services.agent_loop.LLMRouter.stream_openai_raw",
        side_effect=lambda **kw: _fake_stream(**kw),
    ):
        async for item in engine.stream_turn(iteration=iteration):
            if isinstance(item, TurnResult):
                result = item
            else:
                events.append(item)
    return events, result


class TestOpenAITurnEngine:
    @pytest.mark.asyncio
    async def test_text_reassembly_and_deltas(self):
        chunks = [
            _StreamChunk(content="hel"),
            _StreamChunk(content="lo", finish_reason="stop"),
        ]
        events, result = await _run_turn(_engine(), chunks)
        assert [e["content"] for e in events if e["type"] == "text"] == ["hel", "lo"]
        assert result.text == "hello"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []
        assert result.raw_tool_call_count == 0

    @pytest.mark.asyncio
    async def test_tool_call_reassembly_across_chunks(self):
        chunks = [
            _StreamChunk(tool_calls=[_tc(0, "id1", "mytool", '{"q":')]),
            _StreamChunk(
                tool_calls=[_tc(0, None, None, '"x"}')], finish_reason="tool_calls"
            ),
        ]
        _events, result = await _run_turn(_engine(), chunks)
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mytool"
        assert result.tool_calls[0].arguments == '{"q":"x"}'
        assert result.tool_calls[0].id == "id1"
        assert "tool_calls" in result.assistant_message

    @pytest.mark.asyncio
    async def test_empty_name_tool_call_is_skipped_but_counted(self):
        chunks = [
            _StreamChunk(
                tool_calls=[_tc(0, "id1", None, "{}")], finish_reason="tool_calls"
            )
        ]
        _events, result = await _run_turn(_engine(), chunks)
        assert result.tool_calls == []
        assert result.raw_tool_call_count == 1

    @pytest.mark.asyncio
    async def test_missing_id_is_synthesized(self):
        chunks = [
            _StreamChunk(
                tool_calls=[_tc(0, None, "mytool", "{}")], finish_reason="tool_calls"
            )
        ]
        _events, result = await _run_turn(_engine(), chunks)
        assert result.tool_calls[0].id.startswith("call_")

    @pytest.mark.asyncio
    async def test_usage_is_read_from_usage_only_chunk(self):
        chunks = [
            _StreamChunk(content="hi", finish_reason="stop"),
            _usage_chunk(11, 22),
        ]
        _events, result = await _run_turn(_engine(), chunks)
        assert result.input_tokens == 11 and result.output_tokens == 22

    @pytest.mark.asyncio
    async def test_malformed_tool_json_help_on_first_iteration(self):
        text = 'Sure: {"type":"function","name":"x"}'
        chunks = [_StreamChunk(content=text, finish_reason="stop")]
        _events, result = await _run_turn(_engine(), chunks, iteration=0)
        assert result.malformed_help is not None

    @pytest.mark.asyncio
    async def test_no_malformed_help_after_first_iteration(self):
        text = 'Sure: {"type":"function","name":"x"}'
        chunks = [_StreamChunk(content=text, finish_reason="stop")]
        _events, result = await _run_turn(_engine(), chunks, iteration=1)
        assert result.malformed_help is None
