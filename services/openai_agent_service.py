"""OpenAI-protocol agent loop for non-Anthropic providers (Ollama, OpenAI, etc.).

Provides streaming multi-turn tool calling using the OpenAI chat completions
format.
Features:
  - Multi-turn tool loop (up to 30 iterations)
  - Wall-clock timeout (300s)
  - Exponential backoff between iterations
  - Infinite loop detection (identical tool signatures × 3)
  - Per-agent recommended_tools filtering
  - Context window management (sliding history window on the incoming
    conversation)
  - Per-iteration interaction logging (LLMInteractionLog) with token usage
    and cost
  - Tool result truncation + prompt injection wrapping
  - Budget enforcement via VK headers
  - Bifrost correlation UUID per iteration
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from services import tool_manager
from services.llm_router import LLMRouter, ProviderSpec

logger = logging.getLogger(__name__)

_MAX_TOOL_ITERATIONS = 30
_MAX_PROCESSING_TIME_S = 300.0
_TOOL_TIMEOUT_S = 30.0
_MAX_TOOL_RESPONSE_CHARS = 50_000
_HISTORY_WINDOW_DEFAULT = 20
_LOOP_DETECT_WINDOW = 5
_LOOP_DETECT_THRESHOLD = 3
_BASE_INTER_ITERATION_DELAY_S = 0.5
_MAX_INTER_ITERATION_DELAY_S = 3.0
_BACKOFF_ITERATION_THRESHOLD = 15


def anthropic_tools_to_openai(
    tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert Anthropic tool schema format to OpenAI function-calling format.

    Anthropic: {"name", "description", "input_schema": {json-schema}}
    OpenAI:    {"type": "function", "function": {"name", "description",
               "parameters": {json-schema}}}
    """
    converted = []
    for tool in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema",
                        {"type": "object", "properties": {}},
                    ),
                },
            }
        )
    return converted


def _truncate(text: str, max_chars: int = _MAX_TOOL_RESPONSE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n...[truncated, {omitted} chars omitted]"


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


class OpenAIAgentService:
    """Streaming agent loop for OpenAI-compatible providers via Bifrost.

    Mirrors ClaudeService.chat_stream capabilities:
      - Multi-turn tool calling with streaming
      - Infinite loop detection
      - Context window management
      - Per-iteration audit logging
      - Agent-specific tool filtering
    """

    def __init__(
        self,
        *,
        backend_tools: Optional[List[Dict[str, Any]]] = None,
        include_mcp_tools: bool = True,
        recommended_tools: Optional[List[str]] = None,
    ):
        self._backend_tools = backend_tools or self._load_backend_tools()
        self._include_mcp_tools = include_mcp_tools
        self._recommended_tools = recommended_tools
        self._backend_tool_names: Set[str] = {t["name"] for t in self._backend_tools}
        self._refresh_skill_tools()

    @staticmethod
    def _load_backend_tools() -> List[Dict[str, Any]]:
        return tool_manager.load_backend_tools()

    def _refresh_skill_tools(self) -> None:
        """Load DB-backed skill tools via the shared tool manager."""
        skill_tools, self._skill_tool_index = tool_manager.load_skill_tools()
        self._backend_tools.extend(skill_tools)
        self._backend_tool_names.update(t["name"] for t in skill_tools)

    def _get_mcp_tools(self) -> List[Dict[str, Any]]:
        if not self._include_mcp_tools:
            return []
        try:
            from services.mcp_client import get_mcp_client

            client = get_mcp_client()
            if client:
                return client.get_tools_for_claude()
        except Exception as exc:
            logger.debug("MCP tools unavailable: %s", exc)
        return []

    def _filter_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter tools by recommended_tools list (agent-specific subset)."""
        return tool_manager.filter_tools_by_recommended(tools, self._recommended_tools)

    def _all_tools_openai_format(self) -> List[Dict[str, Any]]:
        """Collect backend + MCP tools, filter, and convert to OpenAI format."""
        anthropic_tools = list(self._backend_tools) + self._get_mcp_tools()
        anthropic_tools = self._filter_tools(anthropic_tools)
        if not anthropic_tools:
            return []
        return anthropic_tools_to_openai(anthropic_tools)

    def tools_available(self) -> bool:
        """True if any tool is exposed to the model after filtering.

        Lets the router fall back to the no-tools stream path when the model
        claims tool support but nothing is actually loadable.
        """
        return bool(self._all_tools_openai_format())

    @staticmethod
    def _apply_history_window(
        messages: List[Dict[str, Any]],
        window: int = _HISTORY_WINDOW_DEFAULT,
    ) -> List[Dict[str, Any]]:
        """Enforce a sliding history window (configurable max turns).

        Keeps the system message (if first) + the most recent `window * 2`
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

    async def stream(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        enable_tools: bool = True,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        history_window: int = _HISTORY_WINDOW_DEFAULT,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream a multi-turn agentic conversation with tool calling.

        Yields SSE-compatible event dicts matching the frontend protocol:
            {"type": "text", "content": "..."}
            {"type": "tool_processing", "tool_name": "...",
             "tool_id": "..."}
            {"type": "tool_result", "tool_name": "...",
             "tool_id": "...", "result": "..."}
            {"type": "error", "content": "..."}

        Matches ClaudeService.chat_stream guardrails:
            - 30 tool iterations max
            - 300s wall-clock timeout
            - Exponential inter-iteration backoff
            - Infinite loop detection (3 repeated identical tool sets)
        """
        from openai import AsyncOpenAI

        router = LLMRouter()
        model = model or provider.default_model
        oai_model = f"{provider.provider_type}/{model}"

        tools = self._all_tools_openai_format() if enable_tools else []

        oai_messages: List[Dict[str, Any]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        oai_messages.extend(messages)

        # Context window management
        oai_messages = self._apply_history_window(oai_messages, history_window)

        client = AsyncOpenAI(
            base_url=f"{router.bifrost_url}/v1",
            api_key="bifrost",
        )

        extra_headers = self._build_headers()
        start_time = asyncio.get_event_loop().time()

        # Infinite loop detection state
        tool_call_history: deque = deque(maxlen=_LOOP_DETECT_WINDOW)

        try:
            for iteration in range(_MAX_TOOL_ITERATIONS):
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > _MAX_PROCESSING_TIME_S:
                    yield {
                        "type": "text",
                        "content": (
                            "\n\n[Maximum processing time "
                            f"({_MAX_PROCESSING_TIME_S:.0f}s) exceeded "
                            f"after {iteration} iterations.]"
                        ),
                    }
                    break

                if iteration > 0:
                    delay = _inter_iteration_delay(iteration)
                    await asyncio.sleep(delay)

                interaction_id = str(uuid.uuid4())
                headers = {
                    **extra_headers,
                    "x-bf-lh-vigil-interaction-id": interaction_id,
                }

                kwargs: Dict[str, Any] = {
                    "model": oai_model,
                    "messages": oai_messages,
                    "max_tokens": max_tokens,
                    "stream": True,
                    # Ask for a final usage-only chunk so token counts (and thus
                    # cost) are recorded — without this, streamed responses carry
                    # no usage and analytics would show $0 for OpenAI/Groq/Ollama.
                    "stream_options": {"include_usage": True},
                }
                if temperature is not None:
                    kwargs["temperature"] = temperature
                if tools:
                    kwargs["tools"] = tools
                if headers:
                    kwargs["extra_headers"] = headers

                # Accumulate streamed response
                text_buffer = ""
                tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
                finish_reason: Optional[str] = None
                input_tokens = 0
                output_tokens = 0
                iter_start = time.monotonic()

                try:
                    stream = await client.chat.completions.create(**kwargs)
                    async for chunk in stream:
                        # The usage-only chunk (include_usage) arrives with an
                        # empty ``choices`` list, so read usage before skipping it.
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
                                        entry[
                                            "arguments"
                                        ] += tc_delta.function.arguments

                except Exception as exc:
                    logger.error("OpenAI stream error iteration %d: %s", iteration, exc)
                    yield {"type": "error", "content": str(exc)}
                    break

                iter_duration_ms = int((time.monotonic() - iter_start) * 1000)
                cost_usd = self._compute_cost(
                    model, provider.provider_type, input_tokens, output_tokens
                )

                # Persist interaction log (non-fatal)
                self._log_interaction(
                    session_id=session_id,
                    agent_id=agent_id,
                    model=oai_model,
                    iteration=iteration,
                    interaction_id=interaction_id,
                    duration_ms=iter_duration_ms,
                    text_content=text_buffer,
                    tool_calls_count=len(tool_calls_buffer),
                    finish_reason=finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )

                # No tool calls → done
                if finish_reason != "tool_calls" or not tool_calls_buffer:
                    # Detect malformed tool calls dumped as text (common with
                    # smaller models that attempt tool use but fail at structured
                    # output). Filter it rather than passing hallucinated JSON.
                    if (
                        text_buffer
                        and iteration == 0
                        and (
                            '{"type":"function"' in text_buffer
                            or (
                                '"function"' in text_buffer
                                and '"parameters"' in text_buffer
                            )
                        )
                    ):
                        logger.warning(
                            "Model output contains raw tool-call JSON in text "
                            "(model may not support structured tool calling)"
                        )
                        yield {
                            "type": "text",
                            "content": (
                                "I attempted to use tools but this model doesn't "
                                "reliably support structured tool calling. Please "
                                "select a more capable model in Settings > AI Config "
                                "(e.g., sec8-tools, gpt-4o, or any 7B+ model with "
                                "tool support)."
                            ),
                        }
                    break

                # Build assistant message with tool_calls
                assistant_msg: Dict[str, Any] = {"role": "assistant"}
                if text_buffer:
                    assistant_msg["content"] = text_buffer
                assistant_tool_calls = []
                for idx in sorted(tool_calls_buffer.keys()):
                    tc = tool_calls_buffer[idx]
                    name = (tc["name"] or "").strip()
                    if not name:
                        # A tool-call slot that never received a function name is
                        # unusable — skip it rather than emit a malformed call the
                        # provider will reject.
                        logger.warning(
                            "Skipping tool call %d with empty name (id=%r)",
                            idx,
                            tc["id"],
                        )
                        continue
                    assistant_tool_calls.append(
                        {
                            # Some providers omit the streamed id; synthesize a stable
                            # one so tool results can be correlated back to the call.
                            "id": tc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {"name": name, "arguments": tc["arguments"]},
                        }
                    )

                if not assistant_tool_calls:
                    # Every tool-call slot was malformed. Surface any assistant
                    # text and stop rather than loop on an empty turn.
                    if "content" in assistant_msg:
                        oai_messages.append(assistant_msg)
                    break

                assistant_msg["tool_calls"] = assistant_tool_calls
                oai_messages.append(assistant_msg)

                # Infinite loop detection (canonicalized args so cosmetic
                # streaming differences don't defeat repeat detection)
                call_signature = frozenset(
                    "{}:{}".format(
                        tc["function"]["name"],
                        _canonical_args(tc["function"]["arguments"]),
                    )
                    for tc in assistant_tool_calls
                )
                tool_call_history.append(call_signature)
                if self._detect_infinite_loop(tool_call_history):
                    yield {
                        "type": "text",
                        "content": (
                            "\n\n[Stopping: repeated identical tool calls "
                            "detected (possible infinite loop).]"
                        ),
                    }
                    break

                # Execute each tool and append results
                for tc in assistant_tool_calls:
                    tool_name = tc["function"]["name"]
                    tool_call_id = tc["id"]
                    raw_args = tc["function"]["arguments"]

                    yield {
                        "type": "tool_processing",
                        "tool_name": tool_name,
                        "tool_id": tool_call_id,
                    }

                    result_text, is_error = await self._execute_tool(
                        tool_name, raw_args
                    )

                    preview = result_text[:500] + (
                        "…" if len(result_text) > 500 else ""
                    )
                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_id": tool_call_id,
                        "result": preview,
                        "is_error": is_error,
                    }

                    # OpenAI tool messages carry no is_error field, so mark failures
                    # inline — otherwise the model treats an error as a valid result.
                    oai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                f"ERROR: {result_text}" if is_error else result_text
                            ),
                        }
                    )
            else:
                # for/else: loop exhausted without break
                yield {
                    "type": "text",
                    "content": (
                        f"\n\n[Tool iteration limit ({_MAX_TOOL_ITERATIONS}) "
                        "reached. Stopping.]"
                    ),
                }
        finally:
            await client.close()

    @staticmethod
    def _detect_infinite_loop(history: deque) -> bool:
        """Detect if the same tool call set repeats >= threshold times."""
        if len(history) < _LOOP_DETECT_THRESHOLD:
            return False
        recent = list(history)[-_LOOP_DETECT_THRESHOLD:]
        return len(set(recent)) == 1

    async def _execute_tool(
        self, tool_name: str, raw_arguments: str
    ) -> tuple[str, bool]:
        """Dispatch a tool call to the backend or MCP layer.

        Returns ``(result_text, is_error)`` so the caller can flag failures
        to both the frontend and the model.
        """
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return f"invalid JSON arguments: {raw_arguments[:200]}", True

        if tool_name in self._backend_tool_names:
            return await self._execute_backend_tool(tool_name, arguments)
        return await self._execute_mcp_tool(tool_name, arguments)

    async def _execute_backend_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, bool]:
        """Execute a backend tool via the shared dispatch (same as ClaudeService).

        Delegates to ``tool_manager.execute_backend_tool`` so the tool-name ->
        handler mapping stays identical across both agent loops.
        """
        try:
            result, handled = await tool_manager.execute_backend_tool(
                tool_name, arguments, skill_index=self._skill_tool_index
            )
            if not handled:
                return f"Unknown backend tool: {tool_name}", True
            return _truncate(json.dumps(result, default=str)), False
        except Exception as exc:
            logger.error("Backend tool %s failed: %s", tool_name, exc)
            return f"Error executing {tool_name}: {exc}", True

    async def _execute_mcp_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, bool]:
        """Execute an MCP tool via the shared MCP client."""
        try:
            from services.mcp_client import get_mcp_client
            from services.prompt_security import wrap_tool_result

            client = get_mcp_client()
            if not client:
                return f"MCP client unavailable for tool: {tool_name}", True

            parts = tool_name.split("_", 1)
            if len(parts) == 2:
                server_name, actual_tool = parts
            else:
                server_name = self._find_tool_server(client, tool_name)
                actual_tool = tool_name

            if not server_name:
                return f"No MCP server found for tool: {tool_name}", True

            result = await client.call_tool(
                server_name,
                actual_tool,
                arguments,
                timeout=_TOOL_TIMEOUT_S,
            )

            if isinstance(result, dict):
                content_blocks = result.get(
                    "content",
                    [{"type": "text", "text": str(result)}],
                )
            else:
                content_blocks = [{"type": "text", "text": str(result)}]

            texts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = _truncate(block["text"])
                    text = wrap_tool_result(text, source=server_name, tool=actual_tool)
                    texts.append(text)

            return ("\n".join(texts) if texts else str(result)), False

        except Exception as exc:
            logger.error("MCP tool %s failed: %s", tool_name, exc)
            return f"Error executing {tool_name}: {exc}", True

    @staticmethod
    def _find_tool_server(client: Any, tool_name: str) -> Optional[str]:
        for srv_name, tools in client.tools_cache.items():
            if any(t["name"] == tool_name for t in tools):
                return srv_name
        return None

    @staticmethod
    def _build_headers() -> Dict[str, str]:
        headers: Dict[str, str] = {}
        try:
            from services.budget_service import get_active_vk, should_enforce

            if should_enforce():
                vk = get_active_vk()
                if vk:
                    headers["x-bf-vk"] = vk
        except Exception:
            pass
        return headers

    @staticmethod
    def _compute_cost(
        model: str,
        provider_type: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """USD cost for a single call via the shared pricing helper.

        Uses the same ``compute_call_cost`` (model-registry pricing) the
        Anthropic path uses, so OpenAI/Groq/Ollama spend lands in the same
        analytics buckets. Returns 0.0 if pricing can't be resolved.
        """
        try:
            from daemon.agent_runner import compute_call_cost

            return compute_call_cost(
                model,
                provider_type,
                int(input_tokens or 0),
                int(output_tokens or 0),
            )
        except Exception:
            return 0.0

    @staticmethod
    def _log_interaction(
        *,
        session_id: Optional[str],
        agent_id: Optional[str],
        model: str,
        iteration: int,
        interaction_id: str,
        duration_ms: int,
        text_content: str,
        tool_calls_count: int,
        finish_reason: Optional[str],
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Persist an LLMInteractionLog row (non-fatal, fire-and-forget)."""
        try:
            from database.connection import get_db_session
            from database.models import LLMInteractionLog

            session = get_db_session()
            try:
                log = LLMInteractionLog(
                    interaction_id=interaction_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    model=model,
                    request_messages=[],
                    thinking_enabled=False,
                    response_content=(text_content[:2000] if text_content else None),
                    tool_calls=(
                        [{"iteration": iteration}] if tool_calls_count > 0 else []
                    ),
                    tool_results=[],
                    stop_reason=finish_reason,
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=float(cost_usd or 0.0),
                    duration_ms=duration_ms,
                )
                session.add(log)
                session.commit()
            except Exception as exc:
                # DB is reachable but the insert failed — worth surfacing,
                # since it means audit/cost rows are being silently dropped.
                logger.warning("Interaction log insert failed: %s", exc)
                session.rollback()
            finally:
                session.close()
        except Exception as exc:
            # DB simply unavailable (e.g. dev without Postgres) — expected.
            logger.debug("Interaction log skipped: %s", exc)
