from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from services import tool_manager

# The provider-agnostic loop skeleton (guards, backoff, loop-detection, tool
# exec/approval orchestration, telemetry) now lives in services.agent_loop. This
# service supplies the OpenAI TurnEngine plus the tool-execution runtime the
# controller calls back into. Several private names are re-exported here so
# existing imports (and their tests) keep resolving from this module.
from services.agent_loop import _HISTORY_WINDOW_DEFAULT  # noqa: F401
from services.agent_loop import (
    _LOOP_DETECT_THRESHOLD,
    LoopController,
    OpenAITurnEngine,
    PendingApprovalError,
    _canonical_args,
    _detect_infinite_loop,
)
from services.llm_format import anthropic_tools_to_openai
from services.llm_router import ProviderSpec

logger = logging.getLogger(__name__)

__all__ = [
    "OpenAIAgentService",
    "PendingApprovalError",
    "anthropic_tools_to_openai",
    # Re-exported for compatibility with existing test imports.
    "_canonical_args",
    "_LOOP_DETECT_THRESHOLD",
]

_TOOL_TIMEOUT_S = 30.0
_MAX_TOOL_RESPONSE_CHARS = 50_000
_APPROVAL_POLL_INTERVAL_S = 5.0
_APPROVAL_WAIT_TIMEOUT_S = 600.0


def _truncate(text: str, max_chars: int = _MAX_TOOL_RESPONSE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n...[truncated, {omitted} chars omitted]"


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
        # Attribution context for approval requests; set per-run in stream().
        self._session_id: Optional[str] = None
        self._agent_id: Optional[str] = None
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
        # Stash attribution context for tool gating (approval requests) raised
        # deep in the loop without threading it through every call. The
        # controller drives this instance as its ``runtime`` — the approval and
        # tool-exec callbacks below read these.
        self._session_id = session_id
        self._agent_id = agent_id

        model = model or provider.default_model
        tools = self._all_tools_openai_format() if enable_tools else []

        # Provider-specific model I/O lives in the engine; the provider-agnostic
        # loop skeleton (guards, backoff, loop-detection, tool-exec/approval
        # orchestration, telemetry) lives in the controller, which calls back
        # into this service for tool execution, approval, cost, and logging.
        engine = OpenAITurnEngine(
            provider=provider,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            runtime=self,
            session_id=session_id,
            agent_id=agent_id,
            history_window=history_window,
        )
        controller = LoopController(engine=engine)
        async for event in controller.run():
            yield event

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
        """Run the agentic loop to completion and return the final text.

        Non-streaming convenience for callers (the workflow engine) that want a
        single string the way ``ClaudeService.chat`` returns one. Tool
        processing/result events are consumed internally; only assistant text
        is accumulated. A surfaced ``error`` event is raised so the caller can
        fall back or report failure rather than silently returning "".
        """
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
        """Detect if the same tool call set repeats >= threshold times.

        Kept as a thin passthrough to the controller's implementation so
        existing callers/tests that reach for it on this class still resolve.
        """
        return _detect_infinite_loop(history)

    async def _execute_tool(
        self, tool_name: str, raw_arguments: str, *, approved: bool = False
    ) -> tuple[str, bool]:
        """Dispatch a tool call to the backend or MCP layer, returning
        ``(result_text, is_error)``. ``approved`` skips the approval gate (never
        the forbidden check) to re-run a call an operator has cleared."""
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
        """Queue a pending approval instead of executing the tool, returning the
        message for the model and the action id to poll (None if not created)."""
        try:
            from services.approval_service import ActionType, get_approval_service

            service = get_approval_service()
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
        """Poll the approval queue until an operator decides, returning
        ``(decision, detail, waited_seconds)``. A vanished action reads as a
        rejection so an uncleared action never executes."""
        from services.approval_service import ActionStatus, get_approval_service

        service = get_approval_service()
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
        """Close out an approved action so the queue reflects what ran."""
        if not action_id:
            return
        try:
            from services.approval_service import get_approval_service

            service = get_approval_service()
            if is_error:
                service.mark_failed(action_id, result_text[:2000])
            else:
                service.mark_executed(action_id, {"result": result_text[:2000]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not close out action %s: %s", action_id, exc)

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
            from services.prompt_security import wrap_tool_result

            text = _truncate(json.dumps(result, default=str))
            return wrap_tool_result(text, source="backend", tool=tool_name), False
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
