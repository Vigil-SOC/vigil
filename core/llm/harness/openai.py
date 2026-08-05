from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from services import tool_manager
from services.approval_service import ApprovalService
from services.mcp_client import process_mcp_client
from core.llm.router.format import anthropic_tools_to_openai
from core.llm.router.router import LLMRouter, ProviderSpec

logger = logging.getLogger(__name__)

__all__ = ["OpenAIAgentService", "anthropic_tools_to_openai"]

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
_APPROVAL_POLL_INTERVAL_S = 5.0
_APPROVAL_WAIT_TIMEOUT_S = 600.0


class PendingApprovalError(Exception):
    def __init__(self, message: str, action_id: Optional[str] = None):
        super().__init__(message)
        self.action_id = action_id


def _truncate(text: str, max_chars: int = _MAX_TOOL_RESPONSE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n...[truncated, {omitted} chars omitted]"


def _inter_iteration_delay(iteration: int) -> float:
    if iteration <= _BACKOFF_ITERATION_THRESHOLD:
        return _BASE_INTER_ITERATION_DELAY_S
    exp = iteration - _BACKOFF_ITERATION_THRESHOLD
    delay = _BASE_INTER_ITERATION_DELAY_S * (1.5**exp)
    return min(delay, _MAX_INTER_ITERATION_DELAY_S)


def _canonical_args(raw: str) -> str:
    try:
        return json.dumps(
            json.loads(raw or "{}"), sort_keys=True, separators=(",", ":")
        )
    except (json.JSONDecodeError, TypeError):
        return (raw or "").strip()


class OpenAIAgentService:
    def __init__(
        self,
        *,
        backend_tools: Optional[List[Dict[str, Any]]] = None,
        include_mcp_tools: bool = True,
        recommended_tools: Optional[List[str]] = None,
        mcp_client=None,
        approvals: Optional[ApprovalService] = None,
    ):
        # mcp_client defaults to the process owner's; None when no owner started.
        self._mcp_client = (
            mcp_client if mcp_client is not None else process_mcp_client()
        )
        self._approvals = approvals or ApprovalService()
        self._backend_tools = backend_tools or self._load_backend_tools()
        self._include_mcp_tools = include_mcp_tools
        self._recommended_tools = recommended_tools
        self._backend_tool_names: Set[str] = {t["name"] for t in self._backend_tools}
        # Attribution context for approval requests; set per-run in stream().
        self._session_id: Optional[str] = None
        self._agent_id: Optional[str] = None
        self._refresh_skill_tools()

    @staticmethod
    def _load_backend_tools() -> List[Dict[str, Any]]:
        return tool_manager.load_backend_tools()

    def _refresh_skill_tools(self) -> None:
        skill_tools, self._skill_tool_index = tool_manager.load_skill_tools()
        self._backend_tools.extend(skill_tools)
        self._backend_tool_names.update(t["name"] for t in skill_tools)

    def _get_mcp_tools(self) -> List[Dict[str, Any]]:
        if not self._include_mcp_tools:
            return []
        try:
            if self._mcp_client:
                return self._mcp_client.get_tools_for_claude()
        except Exception as exc:
            logger.debug("MCP tools unavailable: %s", exc)
        return []

    def _filter_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return tool_manager.filter_tools_by_recommended(tools, self._recommended_tools)

    def _all_tools_openai_format(self) -> List[Dict[str, Any]]:
        anthropic_tools = list(self._backend_tools) + self._get_mcp_tools()
        anthropic_tools = self._filter_tools(anthropic_tools)
        if not anthropic_tools:
            return []
        return anthropic_tools_to_openai(anthropic_tools)

    def tools_available(self) -> bool:
        return bool(self._all_tools_openai_format())

    @staticmethod
    def _apply_history_window(
        messages: List[Dict[str, Any]],
        window: int = _HISTORY_WINDOW_DEFAULT,
    ) -> List[Dict[str, Any]]:
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
        # Stash attribution context for tool gating (approval requests) raised
        # deep in the loop without threading it through every call.
        self._session_id = session_id
        self._agent_id = agent_id

        router = LLMRouter()
        model = model or provider.default_model

        tools = self._all_tools_openai_format() if enable_tools else []

        from core.llm.router.format import anthropic_messages_to_openai

        # The loop appends assistant/tool turns in OpenAI shape, so normalize
        # the incoming history up front; the router re-converts idempotently.
        oai_messages = anthropic_messages_to_openai(messages)
        oai_messages = self._apply_history_window(oai_messages, history_window)

        start_time = asyncio.get_event_loop().time()

        # Infinite loop detection state
        tool_call_history: deque = deque(maxlen=_LOOP_DETECT_WINDOW)

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

            # Accumulate streamed response
            text_buffer = ""
            tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
            finish_reason: Optional[str] = None
            input_tokens = 0
            output_tokens = 0
            iter_start = time.monotonic()

            try:
                async for chunk in router.stream_openai_raw(
                    provider=provider,
                    messages=oai_messages,
                    system_prompt=system_prompt,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools or None,
                    interaction_id=interaction_id,
                    # Ask for a final usage-only chunk so token counts (and thus
                    # cost) are recorded — without this, streamed responses carry
                    # no usage and analytics would show $0 for OpenAI/Groq/Ollama.
                    include_usage=True,
                ):
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
                                    entry["arguments"] += tc_delta.function.arguments

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
                model=f"{provider.provider_type}/{model}",
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
                        "Skipping tool call %d with empty name (id=%r)", idx, tc["id"]
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
            halt = False
            for tc in assistant_tool_calls:
                tool_name = tc["function"]["name"]
                tool_call_id = tc["id"]
                raw_args = tc["function"]["arguments"]

                yield {
                    "type": "tool_processing",
                    "tool_name": tool_name,
                    "tool_id": tool_call_id,
                }

                try:
                    result_text, is_error = await self._execute_tool(
                        tool_name, raw_args
                    )
                except PendingApprovalError as exc:
                    # Hold the run here until an operator decides, so a run
                    # never completes with the action un-taken.
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
                    decision, detail, waited = await self._await_approval(exc.action_id)
                    # Human think-time isn't LLM processing time.
                    start_time += waited
                    if decision == "approved":
                        result_text, is_error = await self._execute_tool(
                            tool_name, raw_args, approved=True
                        )
                        self._mark_action_executed(exc.action_id, result_text, is_error)
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
            if halt:
                return
        else:
            # for/else: loop exhausted without break
            yield {
                "type": "text",
                "content": (
                    f"\n\n[Tool iteration limit ({_MAX_TOOL_ITERATIONS}) "
                    "reached. Stopping.]"
                ),
            }

    async def run(
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
    ) -> str:
        parts: List[str] = []
        async for event in self.stream(
            provider=provider,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            enable_tools=enable_tools,
            session_id=session_id,
            agent_id=agent_id,
            history_window=history_window,
        ):
            etype = event.get("type")
            if etype == "text":
                parts.append(event.get("content", ""))
            elif etype == "error":
                raise RuntimeError(event.get("content", "OpenAI agent run failed"))
        return "".join(parts)

    @staticmethod
    def _detect_infinite_loop(history: deque) -> bool:
        if len(history) < _LOOP_DETECT_THRESHOLD:
            return False
        recent = list(history)[-_LOOP_DETECT_THRESHOLD:]
        return len(set(recent)) == 1

    async def _execute_tool(
        self, tool_name: str, raw_arguments: str, *, approved: bool = False
    ) -> tuple[str, bool]:
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return f"invalid JSON arguments: {raw_arguments[:200]}", True

        # Safety gating — same tier policy the daemon enforces. Action tools
        # must never execute on the OpenAI path without a decision: forbidden
        # tools are refused outright, requires_approval tools raise so the
        # caller can hold the run until a human resolves the request.
        tier = tool_manager.get_tool_tier(tool_name)
        if tier == "forbidden":
            logger.warning("Blocked forbidden tool call: %s", tool_name)
            return (
                f"Tool '{tool_name}' is forbidden for autonomous agents and "
                "was not executed.",
                True,
            )
        if tier == "requires_approval" and not approved:
            msg, action_id = await self._request_approval(tool_name, arguments)
            raise PendingApprovalError(msg, action_id)

        if tool_name in self._backend_tool_names:
            return await self._execute_backend_tool(tool_name, arguments)
        return await self._execute_mcp_tool(tool_name, arguments)

    async def _request_approval(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, Optional[str]]:
        try:
            from services.approval_service import ActionType

            service = self._approvals
            try:
                action_type = ActionType(tool_name)
            except ValueError:
                action_type = ActionType.CUSTOM

            target = arguments.get(
                "target", arguments.get("ip", arguments.get("host", "unknown"))
            )
            pending = await asyncio.to_thread(
                service.create_action,
                action_type=action_type,
                title=f"Agent tool: {tool_name}",
                description=(
                    f"Agent (session={self._session_id}, agent={self._agent_id}) "
                    f"requests execution of {tool_name}"
                ),
                target=target,
                confidence=0.7,
                reason=f"Agent needs to execute {tool_name}",
                evidence=[self._session_id or self._agent_id or "chat"],
                created_by=self._agent_id or "openai_agent",
                parameters=arguments,
            )
            return (
                f"Tool '{tool_name}' requires human approval before it can run. "
                f"An approval request was created (action_id={pending.action_id}); "
                "the run is paused until an operator decides.",
                pending.action_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create approval for %s: %s", tool_name, exc)
            return (
                f"Tool '{tool_name}' requires approval, but the approval request "
                f"could not be created ({exc}). The action was not executed.",
                None,
            )

    async def _await_approval(
        self, action_id: str, *, timeout: float = _APPROVAL_WAIT_TIMEOUT_S
    ) -> tuple[str, str, float]:
        from services.approval_service import ActionStatus

        service = self._approvals
        started = time.monotonic()
        deadline = started + timeout
        while True:
            try:
                action = await asyncio.to_thread(service.get_action, action_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Approval poll failed for %s: %s", action_id, exc)
                action = None
            waited = time.monotonic() - started
            if action is None:
                return (
                    "rejected",
                    f"approval request {action_id} is no longer available",
                    waited,
                )
            # PendingAction.status is the ActionStatus *value*, not the enum.
            status = str(action.status)
            if status == ActionStatus.REJECTED.value:
                return (
                    "rejected",
                    action.rejection_reason or "no reason given",
                    waited,
                )
            if status in (
                ActionStatus.APPROVED.value,
                ActionStatus.EXECUTED.value,
            ):
                return "approved", status, waited
            if time.monotonic() >= deadline:
                return (
                    "timeout",
                    f"Timed out after {timeout:.0f}s waiting for approval of "
                    f"action {action_id}. The action was not executed; the run "
                    "is stopping with the step incomplete.",
                    waited,
                )
            await asyncio.sleep(_APPROVAL_POLL_INTERVAL_S)

    @staticmethod
    def _mark_action_executed(
        action_id: Optional[str], result_text: str, is_error: bool
    ) -> None:
        if not action_id:
            return
        try:
            service = self._approvals
            if is_error:
                service.mark_failed(action_id, result_text[:2000])
            else:
                service.mark_executed(action_id, {"result": result_text[:2000]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not close out action %s: %s", action_id, exc)

    async def _execute_backend_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, bool]:
        try:
            result, handled = await tool_manager.execute_backend_tool(
                tool_name, arguments, skill_index=self._skill_tool_index
            )
            if not handled:
                return f"Unknown backend tool: {tool_name}", True
            from core.llm.security import wrap_tool_result

            text = _truncate(json.dumps(result, default=str))
            return wrap_tool_result(text, source="backend", tool=tool_name), False
        except Exception as exc:
            logger.error("Backend tool %s failed: %s", tool_name, exc)
            return f"Error executing {tool_name}: {exc}", True

    async def _execute_mcp_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> tuple[str, bool]:
        try:
            from core.llm.security import wrap_tool_result

            client = self._mcp_client
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
    def _compute_cost(
        model: str,
        provider_type: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        try:
            from core.llm.cost.calls import compute_call_cost

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
