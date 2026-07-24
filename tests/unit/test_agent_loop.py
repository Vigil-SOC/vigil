"""Unit tests for the provider-agnostic agent loop (services/agent_loop.py).

Two seams are covered in isolation:

  - ``LoopController`` — the provider-agnostic *sequencer*: iteration/wall-clock
    guards, backoff, loop-detection, the stop/limit messages, and forwarding of
    the engine's ``stream_turn`` deltas and ``execute_tools`` events. Driven here
    against a *fake* engine so the control flow is tested without any provider.
  - ``OpenAITurnEngine`` — the OpenAI dialect: delta reassembly + telemetry
    (``stream_turn``) and the per-tool gated execution phase (``execute_tools``),
    driven with a mocked ``stream_openai_raw`` and a fake runtime.

The end-to-end behaviour parity guard lives in test_openai_agent_service.py;
this file adds focused coverage of the controller/engine seam.
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
                                 PendingApprovalError, ToolPhaseResult,
                                 TurnResult, _canonical_args,
                                 _inter_iteration_delay)

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
# TurnResult factory
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# LoopController — driven by a fake engine
# --------------------------------------------------------------------------
class _FakeEngine:
    """Scripts a sequence of turns and their tool-phase outcomes.

    The controller sequences turns; this fake supplies the two engine hooks the
    controller drives (``stream_turn`` and ``execute_tools``) plus the telemetry
    fields it reads. Telemetry and tool execution are the engine's job now, so
    the fake just records that ``execute_tools`` ran.
    """

    provider_type = "ollama"
    model = "llama3.1:8b"
    model_label = "ollama/llama3.1:8b"

    def __init__(self, turns, *, tool_phase=None, deltas_per_turn=None):
        # tool_phase: ToolPhaseResult (or callable(turn)->ToolPhaseResult) the
        # engine returns from execute_tools; defaults to a no-op continue.
        self._turns = list(turns)
        if deltas_per_turn is None:
            deltas_per_turn = [
                [{"type": "text", "content": t.text}] if t.text else [] for t in turns
            ]
        self._deltas = deltas_per_turn
        self._tool_phase = tool_phase
        self._i = 0
        self.appended_assistant = []
        self.tool_phase_turns = []

    async def stream_turn(self, *, iteration):
        idx = min(self._i, len(self._turns) - 1)
        for delta in self._deltas[idx] if idx < len(self._deltas) else []:
            yield delta
        turn = self._turns[idx]
        self._i += 1
        yield turn

    def append_assistant(self, turn):
        self.appended_assistant.append(turn)

    async def execute_tools(self, turn, *, iteration):
        self.tool_phase_turns.append(turn)
        # Emit the same provider-shaped events a real engine would, so callers
        # can assert on forwarding.
        for tc in turn.tool_calls:
            yield {"type": "tool_processing", "tool_name": tc.name, "tool_id": tc.id}
            yield {
                "type": "tool_result",
                "tool_name": tc.name,
                "tool_id": tc.id,
                "result": "ok",
                "is_error": False,
            }
        phase = self._tool_phase
        if callable(phase):
            phase = phase(turn)
        yield phase if phase is not None else ToolPhaseResult()


async def _drive(engine, **kwargs):
    controller = LoopController(engine=engine, **kwargs)
    return [ev async for ev in controller.run()]


@pytest.mark.asyncio
async def test_text_only_turn_stops_without_tools():
    engine = _FakeEngine(
        [_turn(text="hi", finish_reason="stop")],
        deltas_per_turn=[[{"type": "text", "content": "hi"}]],
    )
    events = await _drive(engine)
    assert [e for e in events if e["type"] == "text"] == [
        {"type": "text", "content": "hi"}
    ]
    assert not any(e["type"] == "tool_processing" for e in events)
    assert engine.tool_phase_turns == []  # execute_tools never invoked


@pytest.mark.asyncio
async def test_tool_turn_delegates_to_engine_then_finishes():
    tc = NormalizedToolCall(id="call1", name="mytool", arguments='{"q":"x"}')
    engine = _FakeEngine(
        [
            _turn(tool_calls=[tc], finish_reason="tool_calls"),
            _turn(text="done", finish_reason="stop"),
        ]
    )
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine)
    assert [
        e["type"] for e in events if e["type"] in ("tool_processing", "tool_result")
    ] == ["tool_processing", "tool_result"]
    assert len(engine.tool_phase_turns) == 1  # execute_tools ran once
    assert engine.appended_assistant  # assistant appended before the tool phase
    assert any(e.get("content") == "done" for e in events)


@pytest.mark.asyncio
async def test_halt_from_tool_phase_stops_run_before_limit_message():
    tc = NormalizedToolCall(id="c", name="t", arguments="{}")
    engine = _FakeEngine(
        [_turn(tool_calls=[tc], finish_reason="tool_calls")],
        tool_phase=ToolPhaseResult(halt=True),
    )
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine)
    # halt => return, so neither the iteration-limit nor another turn happens.
    assert not any("iteration limit" in e.get("content", "").lower() for e in events)


@pytest.mark.asyncio
async def test_loop_detection_halts_on_repeats():
    tc = NormalizedToolCall(id="c", name="mytool", arguments='{"q":"x"}')
    engine = _FakeEngine(
        [_turn(tool_calls=[tc], finish_reason="tool_calls") for _ in range(6)]
    )
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine)
    assert any(e["type"] == "text" and "infinite loop" in e["content"] for e in events)


@pytest.mark.asyncio
async def test_iteration_limit_message():
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
    with patch("services.agent_loop.asyncio.sleep", new=AsyncMock()):
        events = await _drive(engine, max_iterations=3)
    assert events[-1]["type"] == "text"
    assert "iteration limit" in events[-1]["content"].lower()


@pytest.mark.asyncio
async def test_wall_clock_guard_stops_immediately():
    engine = _FakeEngine([_turn(text="never", finish_reason="stop")])
    events = await _drive(engine, max_processing_time_s=-1.0)
    assert events[-1]["type"] == "text"
    assert "maximum processing time" in events[-1]["content"].lower()
    assert engine.tool_phase_turns == []


@pytest.mark.asyncio
async def test_stream_error_is_surfaced_and_stops():
    class _BoomEngine(_FakeEngine):
        async def stream_turn(self, *, iteration):
            yield {"type": "text", "content": "partial"}
            raise RuntimeError("provider exploded")

    engine = _BoomEngine([_turn()])
    events = await _drive(engine)
    assert events[-1] == {"type": "error", "content": "provider exploded"}
    assert {"type": "text", "content": "partial"} in events


# --------------------------------------------------------------------------
# OpenAITurnEngine — delta reassembly + telemetry (stream_turn)
# --------------------------------------------------------------------------
class _StreamChunk:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
        self.usage = None


def _usage_chunk(prompt, completion):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return SimpleNamespace(choices=[], usage=usage)


def _tc(index, tc_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=tc_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _fake_runtime(**overrides):
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


def _engine(runtime=None, tools=None, messages=None):
    return OpenAITurnEngine(
        provider=SimpleNamespace(provider_type="ollama", default_model="llama3.1:8b"),
        messages=messages or [{"role": "user", "content": "hi"}],
        system_prompt=None,
        model="llama3.1:8b",
        max_tokens=256,
        temperature=None,
        tools=tools or [],
        runtime=runtime or _fake_runtime(),
    )


async def _run_turn(engine, chunks, iteration=0):
    async def _fake_stream(**_kwargs):
        for c in chunks:
            yield c

    events, result = [], None
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


class TestOpenAIStreamTurn:
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

    @pytest.mark.asyncio
    async def test_records_telemetry_via_runtime(self):
        rt = _fake_runtime()
        engine = _engine(runtime=rt)
        chunks = [_StreamChunk(content="hi", finish_reason="stop")]
        await _run_turn(engine, chunks)
        rt._log_interaction.assert_called_once()
        rt._compute_cost.assert_called_once()
        assert rt._log_interaction.call_args.kwargs["model"] == "ollama/llama3.1:8b"


# --------------------------------------------------------------------------
# OpenAITurnEngine — tool-execution phase (execute_tools)
# --------------------------------------------------------------------------
async def _run_tools(engine, turn):
    events, phase = [], None
    async for item in engine.execute_tools(turn, iteration=0):
        if isinstance(item, ToolPhaseResult):
            phase = item
        else:
            events.append(item)
    return events, phase


class TestOpenAIExecuteTools:
    @pytest.mark.asyncio
    async def test_executes_tool_and_appends_result(self):
        rt = _fake_runtime()
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="call1", name="mytool", arguments='{"q":"x"}')
        events, phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        assert [e["type"] for e in events] == ["tool_processing", "tool_result"]
        assert events[1]["is_error"] is False and events[1]["result"].startswith(
            "RESULT"
        )
        rt._execute_tool.assert_awaited_once_with("mytool", '{"q":"x"}')
        assert phase.halt is False and phase.waited == 0.0
        # tool result appended to the engine's history
        assert engine._history[-1]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_error_flag_propagates(self):
        rt = _fake_runtime(_execute_tool=AsyncMock(return_value=("boom", True)))
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="c", name="mytool", arguments="{}")
        events, _phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        res = [e for e in events if e["type"] == "tool_result"]
        assert res and res[0]["is_error"] is True

    @pytest.mark.asyncio
    async def test_approved_reexecutes_and_reports_wait(self):
        rt = _fake_runtime(
            _execute_tool=AsyncMock(
                side_effect=[
                    PendingApprovalError("needs approval", "ACT-1"),
                    ("isolated", False),
                ]
            ),
            _await_approval=AsyncMock(return_value=("approved", "approved", 4.0)),
        )
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="c", name="isolate_host", arguments='{"host":"h1"}')
        events, phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        assert [e["action_id"] for e in events if e["type"] == "approval_required"] == [
            "ACT-1"
        ]
        assert rt._execute_tool.await_args_list[-1].kwargs == {"approved": True}
        rt._mark_action_executed.assert_called_once()
        assert phase.halt is False and phase.waited == 4.0

    @pytest.mark.asyncio
    async def test_rejected_is_reported_not_reexecuted(self):
        rt = _fake_runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("needs approval", "ACT-1")
            ),
            _await_approval=AsyncMock(return_value=("rejected", "too risky", 0.0)),
        )
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="c", name="isolate_host", arguments='{"host":"h1"}')
        events, phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        res = [e for e in events if e["type"] == "tool_result"]
        assert res and res[0]["is_error"] is True
        assert "REJECTED" in res[0]["result"] and "too risky" in res[0]["result"]
        rt._execute_tool.assert_awaited_once()
        assert phase.halt is False

    @pytest.mark.asyncio
    async def test_timeout_halts_phase(self):
        rt = _fake_runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("needs approval", "ACT-1")
            ),
            _await_approval=AsyncMock(return_value=("timeout", "timed out", 0.0)),
        )
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="c", name="isolate_host", arguments='{"host":"h1"}')
        events, phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        assert events[-1] == {"type": "error", "content": "timed out"}
        assert phase.halt is True

    @pytest.mark.asyncio
    async def test_uncreatable_approval_fails_closed(self):
        await_mock = AsyncMock()
        rt = _fake_runtime(
            _execute_tool=AsyncMock(
                side_effect=PendingApprovalError("queue down", None)
            ),
            _await_approval=await_mock,
        )
        engine = _engine(runtime=rt)
        tc = NormalizedToolCall(id="c", name="isolate_host", arguments='{"host":"h1"}')
        events, phase = await _run_tools(engine, _turn(tool_calls=[tc]))
        assert events[-1]["type"] == "error"
        assert phase.halt is True
        await_mock.assert_not_awaited()
