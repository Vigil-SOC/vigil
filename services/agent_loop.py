"""Provider-agnostic agentic tool loop.

This module houses the two halves of the multi-turn tool loop that used to live
entirely inside ``OpenAIAgentService``:

  - :class:`LoopController` — the **provider-agnostic** loop *sequencer*: only
    the cross-cutting guards every provider shares — the iteration cap, the
    wall-clock timeout, inter-iteration backoff, canonicalized loop-detection,
    and the stop/limit messages. It knows nothing about any specific provider; it
    drives a :class:`TurnEngine` and forwards its events.
  - :class:`TurnEngine` — the interface a provider adapter implements, plus the
    :class:`OpenAITurnEngine` adapter that speaks the OpenAI-compatible dialect
    (delta reassembly over ``LLMRouter.stream_openai_raw``, tool-call
    normalization, per-tool gated execution, telemetry, usage accounting, and the
    malformed-tool-JSON heuristic).

Each engine owns its own message-history dialect, SSE event vocabulary, tool
execution/gating, and telemetry; the controller only ever sees provider-agnostic
:class:`TurnResult` / :class:`ToolPhaseResult` values. This lets a future
``AnthropicTurnEngine`` keep native Anthropic-block history, emit thinking
events, and batch-execute tools without the controller having to know.

Behaviour here is a straight lift of the previous ``OpenAIAgentService.stream``
loop — the split is structural, not behavioural.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Union

from services.llm_format import anthropic_messages_to_openai
from services.llm_router import LLMRouter, ProviderSpec

logger = logging.getLogger(__name__)

__all__ = [
    "PendingApprovalError",
    "NormalizedToolCall",
    "TurnResult",
    "ToolPhaseResult",
    "TurnEngine",
    "OpenAITurnEngine",
    "AnthropicTurnEngine",
    "LoopController",
]

_MAX_TOOL_ITERATIONS = 30
_MAX_PROCESSING_TIME_S = 300.0
_HISTORY_WINDOW_DEFAULT = 20
_LOOP_DETECT_WINDOW = 5
_LOOP_DETECT_THRESHOLD = 3
_BASE_INTER_ITERATION_DELAY_S = 0.5
_MAX_INTER_ITERATION_DELAY_S = 3.0
_BACKOFF_ITERATION_THRESHOLD = 15


class PendingApprovalError(Exception):
    """Tool call blocked pending human approval; action_id is the queue row to
    poll, or None when the request could not be created."""

    def __init__(self, message: str, action_id: Optional[str] = None):
        super().__init__(message)
        self.action_id = action_id


def _inter_iteration_delay(iteration: int) -> float:
    """Exponential backoff matching ClaudeService: 500ms base, ramp after 15."""
    if iteration <= _BACKOFF_ITERATION_THRESHOLD:
        return _BASE_INTER_ITERATION_DELAY_S
    exp = iteration - _BACKOFF_ITERATION_THRESHOLD
    delay = _BASE_INTER_ITERATION_DELAY_S * (1.5**exp)
    return min(delay, _MAX_INTER_ITERATION_DELAY_S)


def _canonical_args(raw: str) -> str:
    """Canonicalize tool-call argument JSON for stable loop detection.

    Parse and re-serialize with sorted keys so cosmetic streaming
    differences (whitespace, key ordering) don't make a repeated call look
    distinct and defeat the infinite-loop guard. Falls back to the stripped
    raw string when the arguments aren't valid JSON.
    """
    try:
        return json.dumps(
            json.loads(raw or "{}"), sort_keys=True, separators=(",", ":")
        )
    except (json.JSONDecodeError, TypeError):
        return (raw or "").strip()


def _detect_infinite_loop(history: deque) -> bool:
    """Detect if the same tool call set repeats >= threshold times."""
    if len(history) < _LOOP_DETECT_THRESHOLD:
        return False
    recent = list(history)[-_LOOP_DETECT_THRESHOLD:]
    return len(set(recent)) == 1


def _apply_history_window(
    messages: List[Dict[str, Any]],
    window: int = _HISTORY_WINDOW_DEFAULT,
) -> List[Dict[str, Any]]:
    """Enforce a sliding history window (configurable max turns).

    Keeps the system message (if first) + the most recent ``window * 2``
    messages. Matches ClaudeService._apply_history_window behavior.
    """
    if window <= 0:
        return messages
    max_msgs = window * 2
    if len(messages) <= max_msgs:
        return messages
    # Preserve system message at index 0 if present
    if messages and messages[0].get("role") == "system":
        keep = max_msgs - 1
        return [messages[0]] + messages[-keep:]
    return messages[-max_msgs:]


@dataclass
class NormalizedToolCall:
    """A provider-agnostic tool call the controller acts on.

    ``arguments`` stays the raw JSON *string* the model emitted so it flows into
    the tool executor unchanged and is canonicalized only for loop-detection.
    """

    id: str
    name: str
    arguments: str


@dataclass
class TurnResult:
    """The terminal outcome of one model turn, produced by a ``TurnEngine``.

    The engine has already streamed any text/thinking deltas to the caller by
    the time it yields this; the controller uses these fields to decide whether
    to execute tools, stop, or emit a diagnostic.
    """

    text: str
    finish_reason: Optional[str]
    # Number of tool-call slots the provider emitted *before* filtering (a slot
    # that never received a name is unusable and dropped from ``tool_calls``, but
    # still counted here so the controller's "did the model try to call a tool?"
    # check matches the pre-refactor behaviour).
    raw_tool_call_count: int
    tool_calls: List[NormalizedToolCall]
    # Provider-shape assistant message to append to history when continuing.
    assistant_message: Optional[Dict[str, Any]]
    # Diagnostic to surface when the model dumped raw tool-call JSON as text
    # instead of making a structured call (populated only on the first turn).
    malformed_help: Optional[str]
    interaction_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


@dataclass
class ToolPhaseResult:
    """The terminal outcome of an engine's tool-execution phase.

    ``halt`` tells the controller to stop the whole run immediately (e.g. an
    approval failed closed or timed out). ``waited`` is human approval think-time
    the controller adds back to its wall-clock baseline so operator delay isn't
    charged against the processing-time guard.
    """

    halt: bool = False
    waited: float = 0.0


class TurnEngine(Protocol):
    """Provider adapter driven by :class:`LoopController`.

    Implementations own their message-history dialect, their provider-shaped SSE
    event vocabulary, their tool execution/gating, and their own telemetry — the
    controller only sequences turns and enforces the cross-cutting guards. Each
    engine is constructed per-run with its full context (session/agent, runtime).

    ``stream_turn`` yields SSE-compatible delta dicts (``text``/``thinking``/…)
    as they arrive, records its own interaction telemetry, and terminates by
    yielding exactly one :class:`TurnResult`. ``execute_tools`` runs the tool
    calls for a turn — yielding provider-shaped tool events, extending history,
    and terminating with a :class:`ToolPhaseResult`.
    """

    #: Resolved model id (for cost lookup).
    model: str
    #: Provider type string, e.g. "ollama"/"openai" (for cost lookup).
    provider_type: str
    #: Human-readable model label for interaction logs, e.g. "ollama/llama3".
    model_label: str

    def stream_turn(
        self, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], TurnResult]]: ...

    def append_assistant(self, turn: TurnResult) -> None: ...

    def execute_tools(
        self, turn: TurnResult, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], ToolPhaseResult]]: ...


class OpenAITurnEngine:
    """OpenAI-compatible :class:`TurnEngine` over ``LLMRouter.stream_openai_raw``.

    Owns an OpenAI chat-completion message history and reassembles streamed
    deltas (text + tool-call fragments) into a :class:`TurnResult`. Lifted from
    the body of the old ``OpenAIAgentService.stream``.
    """

    def __init__(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        max_tokens: int,
        temperature: Optional[float],
        tools: List[Dict[str, Any]],
        runtime: Any,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        history_window: int = _HISTORY_WINDOW_DEFAULT,
        router: Optional[LLMRouter] = None,
    ):
        self._provider = provider
        self._system_prompt = system_prompt
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._tools = tools
        # Tool execution, approval, and telemetry live on the runtime (the
        # OpenAIAgentService instance) — the engine calls back into it, mirroring
        # the PR3a arrangement now that the controller no longer owns that work.
        self._runtime = runtime
        self._session_id = session_id
        self._agent_id = agent_id
        self._router = router or LLMRouter()
        # The loop appends assistant/tool turns in OpenAI shape, so normalize the
        # incoming history up front; the router re-converts idempotently.
        history = anthropic_messages_to_openai(messages)
        self._history = _apply_history_window(history, history_window)

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_type(self) -> str:
        return self._provider.provider_type

    @property
    def model_label(self) -> str:
        return f"{self._provider.provider_type}/{self._model}"

    def append_assistant(self, turn: TurnResult) -> None:
        if turn.assistant_message is not None:
            self._history.append(turn.assistant_message)

    def _append_tool_result(
        self, tool_call_id: str, result_text: str, is_error: bool
    ) -> None:
        # OpenAI tool messages carry no is_error field, so mark failures inline —
        # otherwise the model treats an error as a valid result.
        self._history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": (f"ERROR: {result_text}" if is_error else result_text),
            }
        )

    async def stream_turn(
        self, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], TurnResult]]:
        interaction_id = str(uuid.uuid4())
        text_buffer = ""
        tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        input_tokens = 0
        output_tokens = 0
        iter_start = time.monotonic()

        async for chunk in self._router.stream_openai_raw(
            provider=self._provider,
            messages=self._history,
            system_prompt=self._system_prompt,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            tools=self._tools or None,
            interaction_id=interaction_id,
            # Ask for a final usage-only chunk so token counts (and thus cost)
            # are recorded — without this, streamed responses carry no usage and
            # analytics would show $0 for OpenAI/Groq/Ollama.
            include_usage=True,
        ):
            # The usage-only chunk (include_usage) arrives with an empty
            # ``choices`` list, so read usage before skipping it.
            usage = getattr(chunk, "usage", None)
            if usage:
                input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(usage, "completion_tokens", 0) or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            finish_reason = choice.finish_reason or finish_reason

            if delta and delta.content:
                text_buffer += delta.content
                yield {"type": "text", "content": delta.content}

            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = tool_calls_buffer[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        duration_ms = int((time.monotonic() - iter_start) * 1000)

        # Build the assistant message + normalized tool calls from the buffer.
        assistant_msg: Dict[str, Any] = {"role": "assistant"}
        if text_buffer:
            assistant_msg["content"] = text_buffer
        normalized: List[NormalizedToolCall] = []
        oai_tool_calls: List[Dict[str, Any]] = []
        for idx in sorted(tool_calls_buffer.keys()):
            tc = tool_calls_buffer[idx]
            name = (tc["name"] or "").strip()
            if not name:
                # A tool-call slot that never received a function name is unusable
                # — skip it rather than emit a malformed call the provider rejects.
                logger.warning(
                    "Skipping tool call %d with empty name (id=%r)", idx, tc["id"]
                )
                continue
            # Some providers omit the streamed id; synthesize a stable one so tool
            # results can be correlated back to the call.
            call_id = tc["id"] or f"call_{uuid.uuid4().hex[:12]}"
            oai_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": tc["arguments"]},
                }
            )
            normalized.append(
                NormalizedToolCall(id=call_id, name=name, arguments=tc["arguments"])
            )
        if oai_tool_calls:
            assistant_msg["tool_calls"] = oai_tool_calls

        malformed_help = None
        # Detect malformed tool calls dumped as text (common with smaller models
        # that attempt tool use but fail at structured output). Only relevant on
        # a turn with no actionable tool call — mirror the original loop's gate
        # (``finish_reason != "tool_calls" or not tool_calls_buffer``) so a turn
        # that *did* call a tool never triggers a spurious warning.
        finished = finish_reason != "tool_calls" or not tool_calls_buffer
        if (
            finished
            and text_buffer
            and iteration == 0
            and (
                '{"type":"function"' in text_buffer
                or ('"function"' in text_buffer and '"parameters"' in text_buffer)
            )
        ):
            logger.warning(
                "Model output contains raw tool-call JSON in text "
                "(model may not support structured tool calling)"
            )
            malformed_help = (
                "I attempted to use tools but this model doesn't reliably "
                "support structured tool calling. Please select a more capable "
                "model in Settings > AI Config (e.g., sec8-tools, gpt-4o, or any "
                "7B+ model with tool support)."
            )

        turn = TurnResult(
            text=text_buffer,
            finish_reason=finish_reason,
            raw_tool_call_count=len(tool_calls_buffer),
            tool_calls=normalized,
            assistant_message=assistant_msg,
            malformed_help=malformed_help,
            interaction_id=interaction_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
        # Persist this iteration's interaction log (non-fatal, fire-and-forget)
        # via the runtime, then hand the turn to the controller.
        self._record_turn(turn, iteration)
        yield turn

    def _record_turn(self, turn: TurnResult, iteration: int) -> None:
        """Record per-iteration cost + interaction telemetry via the runtime."""
        cost_usd = self._runtime._compute_cost(
            self._model,
            self._provider.provider_type,
            turn.input_tokens,
            turn.output_tokens,
        )
        self._runtime._log_interaction(
            session_id=self._session_id,
            agent_id=self._agent_id,
            model=self.model_label,
            iteration=iteration,
            interaction_id=turn.interaction_id,
            duration_ms=turn.duration_ms,
            text_content=turn.text,
            tool_calls_count=turn.raw_tool_call_count,
            finish_reason=turn.finish_reason,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cost_usd=cost_usd,
        )

    async def execute_tools(
        self, turn: TurnResult, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], ToolPhaseResult]]:
        """Execute a turn's tool calls, one at a time, through the runtime.

        Each call passes through the runtime's tier/approval gate; a
        ``requires_approval`` tool holds the run until an operator decides.
        Yields ``tool_processing`` / ``tool_result`` / ``approval_required`` /
        ``error`` events and terminates with a :class:`ToolPhaseResult`.
        """
        runtime = self._runtime
        halt = False
        total_waited = 0.0
        for tc in turn.tool_calls:
            tool_name = tc.name
            tool_call_id = tc.id
            raw_args = tc.arguments

            yield {
                "type": "tool_processing",
                "tool_name": tool_name,
                "tool_id": tool_call_id,
            }

            try:
                result_text, is_error = await runtime._execute_tool(tool_name, raw_args)
            except PendingApprovalError as exc:
                # Hold the run here until an operator decides, so a run never
                # completes with the action un-taken.
                yield {
                    "type": "approval_required",
                    "tool_name": tool_name,
                    "tool_id": tool_call_id,
                    "action_id": exc.action_id,
                    "content": str(exc),
                }
                yield {"type": "text", "content": f"\n\n_{exc}_\n\n"}
                if not exc.action_id:
                    # No action to poll — fail closed rather than hang.
                    yield {"type": "error", "content": str(exc)}
                    halt = True
                    break
                decision, detail, waited = await runtime._await_approval(exc.action_id)
                # Human think-time isn't LLM processing time.
                total_waited += waited
                if decision == "approved":
                    result_text, is_error = await runtime._execute_tool(
                        tool_name, raw_args, approved=True
                    )
                    runtime._mark_action_executed(exc.action_id, result_text, is_error)
                elif decision == "rejected":
                    result_text, is_error = (
                        f"Operator REJECTED this action. Reason: {detail}. "
                        "Do not retry it; continue with another approach or "
                        "report that the step could not be completed.",
                        True,
                    )
                else:
                    yield {"type": "error", "content": detail}
                    halt = True
                    break

            preview = result_text[:500] + ("…" if len(result_text) > 500 else "")
            yield {
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_id": tool_call_id,
                "result": preview,
                "is_error": is_error,
            }

            self._append_tool_result(tool_call_id, result_text, is_error)

        yield ToolPhaseResult(halt=halt, waited=total_waited)


class AnthropicTurnEngine:
    """Anthropic-native :class:`TurnEngine` over ``client.messages.stream``.

    Wraps a ``ClaudeService`` instance and reuses its proven helpers
    (``async_client.messages.stream``, ``_process_mixed_tool_use``,
    ``_clean_blocks_for_resend``, ``_persist_interaction``). Lifted from the
    body of ``ClaudeService.chat_stream``:

      - ``stream_turn`` emits ``thinking_start``/``thinking``/``thinking_end`` and
        ``text`` deltas, reads final usage via ``get_final_message()``, and
        persists the reasoning trace — one interaction row per iteration.
      - ``execute_tools`` runs the turn's tool calls as a single batch through
        ``_process_mixed_tool_use`` and appends the Anthropic-block tool results.

    Canonical history is Anthropic content-block shape (native to this engine),
    so no conversion happens at the boundary. Approval gating is intentionally
    absent here — it matches ``chat_stream``'s current behaviour and is unified
    across providers in a later sub-PR.
    """

    def __init__(
        self,
        *,
        runtime: Any,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]],
        thinking_config: Optional[Dict[str, Any]],
        use_thinking: bool,
        thinking_budget: Optional[int] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        investigation_id: Optional[str] = None,
    ):
        self._runtime = runtime
        self._messages = messages
        self._system_prompt = system_prompt
        self._model = model
        self._max_tokens = max_tokens
        self._tools = tools
        self._thinking_config = thinking_config
        self._use_thinking = use_thinking
        self._thinking_budget = thinking_budget
        self._session_id = session_id
        self._agent_id = agent_id
        self._investigation_id = investigation_id
        # Raw response blocks from the most recent turn, handed to execute_tools.
        self._last_accumulated: List[Any] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_type(self) -> str:
        return "anthropic"

    @property
    def model_label(self) -> str:
        return f"anthropic/{self._model}"

    def _build_api_kwargs(self, interaction_id: str) -> Dict[str, Any]:
        api_kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": self._messages,
        }
        if self._system_prompt:
            api_kwargs["system"] = self._system_prompt
        if self._tools:
            api_kwargs["tools"] = self._tools
        if self._thinking_config:
            api_kwargs["thinking"] = self._thinking_config
        # #185: fresh interaction UUID per streaming iteration so each upstream
        # Bifrost call lands its own log row correlated with the local one.
        api_kwargs["extra_headers"] = {"x-bf-lh-vigil-interaction-id": interaction_id}
        return api_kwargs

    async def stream_turn(
        self, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], TurnResult]]:
        interaction_id = str(uuid.uuid4())
        api_kwargs = self._build_api_kwargs(interaction_id)

        stream_started = asyncio.get_event_loop().time()
        accumulated_content: List[Any] = []
        current_thinking_block: List[str] = []
        in_thinking = False
        final_message = None
        stop_reason = None

        async with self._runtime.async_client.messages.stream(**api_kwargs) as stream:
            async for event in stream:
                if not hasattr(event, "type"):
                    continue
                event_type = event.type

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "thinking":
                        in_thinking = True
                        current_thinking_block = []
                        yield {"type": "thinking_start"}

                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None) if delta else None
                    if delta_type == "thinking_delta" and hasattr(delta, "thinking"):
                        thinking_text = getattr(delta, "thinking", "")
                        current_thinking_block.append(thinking_text)
                        yield {"type": "thinking", "content": thinking_text}
                    elif delta_type == "text_delta" and hasattr(delta, "text"):
                        if not in_thinking:
                            text = getattr(delta, "text", "")
                            yield {"type": "text", "content": text}

                elif event_type == "content_block_stop":
                    if in_thinking:
                        in_thinking = False
                        yield {"type": "thinking_end"}

            final_message = await stream.get_final_message()
            accumulated_content = final_message.content
            stop_reason = final_message.stop_reason

        duration_ms = int((asyncio.get_event_loop().time() - stream_started) * 1000)
        usage = getattr(final_message, "usage", None)

        # Persist this iteration's reasoning trace (non-fatal, fire-and-forget).
        try:
            await asyncio.to_thread(
                self._runtime._persist_interaction,
                session_id=self._session_id,
                agent_id=self._agent_id,
                investigation_id=self._investigation_id,
                model=getattr(final_message, "model", self._model),
                system_prompt=self._system_prompt,
                request_messages=self._messages,
                response_content=(
                    list(accumulated_content) if accumulated_content else []
                ),
                thinking_enabled=self._use_thinking,
                thinking_budget=(self._thinking_budget if self._use_thinking else None),
                stop_reason=stop_reason,
                input_tokens=(getattr(usage, "input_tokens", 0) if usage else 0),
                output_tokens=(getattr(usage, "output_tokens", 0) if usage else 0),
                cache_read_tokens=(
                    getattr(usage, "cache_read_input_tokens", 0) if usage else 0
                ),
                cache_creation_tokens=(
                    getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
                ),
                duration_ms=duration_ms,
                interaction_id=interaction_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reasoning-trace persist skipped (stream): %s", exc)

        # Normalize tool_use blocks so the controller can loop-detect and decide.
        normalized: List[NormalizedToolCall] = []
        raw_tool_call_count = 0
        for block in accumulated_content:
            if getattr(block, "type", None) == "tool_use":
                raw_tool_call_count += 1
                name = getattr(block, "name", "") or ""
                tool_input = getattr(block, "input", {}) or {}
                block_id = getattr(block, "id", "") or f"toolu_{uuid.uuid4().hex[:12]}"
                try:
                    arguments = json.dumps(tool_input, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    arguments = str(tool_input)
                normalized.append(
                    NormalizedToolCall(id=block_id, name=name, arguments=arguments)
                )

        # stop_reason "tool_use" is the Anthropic signal for "run tools"; map it
        # to the controller's provider-agnostic "tool_calls" sentinel.
        finish_reason = "tool_calls" if stop_reason == "tool_use" else stop_reason
        assistant_message = {
            "role": "assistant",
            "content": self._runtime._clean_blocks_for_resend(accumulated_content),
        }
        self._last_accumulated = accumulated_content

        yield TurnResult(
            text="",
            finish_reason=finish_reason,
            raw_tool_call_count=raw_tool_call_count,
            tool_calls=normalized,
            assistant_message=assistant_message,
            malformed_help=None,
            interaction_id=interaction_id,
            input_tokens=(getattr(usage, "input_tokens", 0) if usage else 0),
            output_tokens=(getattr(usage, "output_tokens", 0) if usage else 0),
            duration_ms=duration_ms,
        )

    def append_assistant(self, turn: TurnResult) -> None:
        if turn.assistant_message is not None:
            self._messages.append(turn.assistant_message)

    async def execute_tools(
        self, turn: TurnResult, *, iteration: int
    ) -> AsyncIterator[Union[Dict[str, Any], ToolPhaseResult]]:
        """Batch-execute the turn's tool calls via the runtime.

        Mirrors ``chat_stream``: one ``tool_processing`` signal, all tool_use
        blocks routed through ``_process_mixed_tool_use``, and the results
        appended as a single Anthropic ``user`` message. No per-tool approval
        gate today (unified across providers in a later sub-PR).
        """
        yield {"type": "tool_processing"}
        tool_results = await self._runtime._process_mixed_tool_use(
            self._last_accumulated
        )
        self._messages.append({"role": "user", "content": tool_results})
        yield ToolPhaseResult(halt=False, waited=0.0)


class _ToolRuntime(Protocol):
    """The tool-side collaborator the OpenAI engine calls back into.

    This is the ``OpenAIAgentService`` instance (so its existing, unit-tested
    methods — and any test doubles patched onto them — keep being the code that
    runs). The engine owns tool execution and telemetry orchestration; the
    controller no longer touches the runtime.
    """

    async def _execute_tool(
        self, tool_name: str, raw_arguments: str, *, approved: bool = False
    ) -> "tuple[str, bool]": ...

    async def _await_approval(self, action_id: str) -> "tuple[str, str, float]": ...

    def _mark_action_executed(
        self, action_id: Optional[str], result_text: str, is_error: bool
    ) -> None: ...

    def _compute_cost(
        self, model: str, provider_type: str, input_tokens: int, output_tokens: int
    ) -> float: ...

    def _log_interaction(self, **kwargs: Any) -> None: ...


class LoopController:
    """Provider-agnostic multi-turn agentic loop sequencer.

    Owns only the cross-cutting guards shared by every provider: the iteration
    cap, the wall-clock timeout, inter-iteration backoff, canonicalized
    loop-detection, and the stop/limit messages. Everything provider-specific —
    streaming, telemetry, tool execution/gating, history mutation, and the
    provider's SSE event vocabulary — lives in the :class:`TurnEngine` it drives.
    Yields SSE-compatible event dicts matching the frontend protocol.
    """

    def __init__(
        self,
        *,
        engine: TurnEngine,
        max_iterations: int = _MAX_TOOL_ITERATIONS,
        max_processing_time_s: float = _MAX_PROCESSING_TIME_S,
    ):
        self._engine = engine
        self._max_iterations = max_iterations
        self._max_processing_time_s = max_processing_time_s

    async def run(self) -> AsyncIterator[Dict[str, Any]]:
        """Run the agentic loop, yielding SSE event dicts.

        Guardrails (unchanged from the pre-refactor OpenAI loop):
            - iteration cap (default 30)
            - wall-clock timeout (default 300s)
            - exponential inter-iteration backoff
            - infinite loop detection (3 repeated identical tool sets)
        """
        engine = self._engine

        start_time = asyncio.get_event_loop().time()
        tool_call_history: deque = deque(maxlen=_LOOP_DETECT_WINDOW)

        for iteration in range(self._max_iterations):
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > self._max_processing_time_s:
                yield {
                    "type": "text",
                    "content": (
                        "\n\n[Maximum processing time "
                        f"({self._max_processing_time_s:.0f}s) exceeded "
                        f"after {iteration} iterations.]"
                    ),
                }
                break

            if iteration > 0:
                await asyncio.sleep(_inter_iteration_delay(iteration))

            # Stream one model turn; re-yield deltas, capture the terminal result.
            # The engine records its own telemetry before yielding the TurnResult.
            turn: Optional[TurnResult] = None
            try:
                async for item in engine.stream_turn(iteration=iteration):
                    if isinstance(item, TurnResult):
                        turn = item
                    else:
                        yield item
            except Exception as exc:  # noqa: BLE001
                logger.error("Turn stream error iteration %d: %s", iteration, exc)
                yield {"type": "error", "content": str(exc)}
                break

            if turn is None:
                # A well-behaved engine always yields a terminal TurnResult; if
                # one didn't, stop rather than spin.
                break

            # No actionable tool call → done.
            if turn.finish_reason != "tool_calls" or turn.raw_tool_call_count == 0:
                if turn.malformed_help:
                    yield {"type": "text", "content": turn.malformed_help}
                break

            if not turn.tool_calls:
                # Every tool-call slot was malformed (empty name). Surface any
                # assistant text and stop rather than loop on an empty turn.
                if turn.assistant_message and "content" in turn.assistant_message:
                    engine.append_assistant(turn)
                break

            engine.append_assistant(turn)

            # Infinite loop detection (canonicalized args so cosmetic streaming
            # differences don't defeat repeat detection).
            call_signature = frozenset(
                "{}:{}".format(tc.name, _canonical_args(tc.arguments))
                for tc in turn.tool_calls
            )
            tool_call_history.append(call_signature)
            if _detect_infinite_loop(tool_call_history):
                yield {
                    "type": "text",
                    "content": (
                        "\n\n[Stopping: repeated identical tool calls "
                        "detected (possible infinite loop).]"
                    ),
                }
                break

            # Hand the tool-execution phase to the engine; it yields provider-
            # shaped tool events and reports back whether to halt and how much
            # human approval time to exclude from the wall-clock guard.
            phase: Optional[ToolPhaseResult] = None
            async for tool_event in engine.execute_tools(turn, iteration=iteration):
                if isinstance(tool_event, ToolPhaseResult):
                    phase = tool_event
                else:
                    yield tool_event
            if phase is not None:
                start_time += phase.waited
                if phase.halt:
                    return
        else:
            # for/else: loop exhausted without break
            yield {
                "type": "text",
                "content": (
                    f"\n\n[Tool iteration limit ({self._max_iterations}) "
                    "reached. Stopping.]"
                ),
            }
