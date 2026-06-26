"""Context lifecycle management.

Responsibilities:
- Token estimation (char-based heuristic)
- History windowing (sliding window, no LLM call)
- Rolling summary compression (fold aged-out messages into a summary string
  asynchronously; zero LLM calls at request time, provider-agnostic)
- Prompt-cache control injection (Anthropic cache_control blocks)
- Tool filtering and per-tool response budgets
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.defaults import DEFAULT_MODEL

logger = logging.getLogger(__name__)

# Rolling summary is capped to avoid unbounded growth.
_SUMMARY_MAX_CHARS = 8000  # ~2k tokens


def _flatten_content_to_text(content: Any) -> str:
    """Extract plain text from any message content shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    inner = block.get("content", "")
                    parts.append(_flatten_content_to_text(inner))
                elif block.get("type") in ("tool_use",):
                    parts.append(json.dumps(block.get("input", {})))
            elif hasattr(block, "text"):
                parts.append(getattr(block, "text", ""))
        return " ".join(p for p in parts if p)
    return ""


def _prepend_summary_block(summary: str, windowed: List[Dict]) -> List[Dict]:
    if not summary:
        return list(windowed)
    summary_msg = {
        "role": "user",
        "content": f"[INVESTIGATION CONTEXT - rolling summary of earlier conversation]\n{summary}\n[END CONTEXT]",
    }
    # The summary is injected as a synthetic user turn. To preserve the strict
    # user/assistant alternation the Anthropic API requires, only follow it with
    # the synthetic acknowledgement when the windowed history resumes with a user
    # turn. If the window was sliced so it now starts with an assistant turn,
    # that turn already follows the summary user turn correctly — adding the ack
    # would produce two consecutive assistant messages.
    if windowed and windowed[0].get("role") == "assistant":
        return [summary_msg, *windowed]
    return [
        summary_msg,
        {
            "role": "assistant",
            "content": "Context noted. Continuing the investigation.",
        },
        *windowed,
    ]


class ContextManager:
    """Manages token budgets, rolling compression, caching, and tool helpers.

    No LLM calls at request time. Summarisation clients are kept for backward
    compatibility but are not used in the main prepare_context path.
    """

    TOOL_RESPONSE_BUDGETS: Dict[str, int] = {
        "get_raw_logs": 30000,
        "timesketch_search": 30000,
        "splunk_search": 30000,
        "list_findings": 12000,
        "search_findings": 12000,
        "list_cases": 12000,
        "semantic_search_findings": 12000,
        "nearest_neighbors": 12000,
    }

    MAX_TOOL_RESPONSE_TOKENS = 30000

    def __init__(
        self,
        sync_client=None,
        async_client=None,
    ) -> None:
        self._sync_client = sync_client
        self._async_client = async_client

    def update_clients(self, sync_client=None, async_client=None) -> None:
        self._sync_client = sync_client
        self._async_client = async_client

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_tokens(content: Any) -> int:
        if isinstance(content, str):
            return len(content) // 4
        if isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, str):
                    total += len(item) // 4
                elif isinstance(item, dict):
                    if "content" in item:
                        total += ContextManager.estimate_tokens(item["content"])
                    if "text" in item:
                        total += len(item["text"]) // 4
                    if "input" in item:
                        total += len(json.dumps(item["input"])) // 4
                elif hasattr(item, "text"):
                    total += len(getattr(item, "text", "")) // 4
            return total
        return 0

    # ------------------------------------------------------------------
    # History windowing
    # ------------------------------------------------------------------

    @staticmethod
    def apply_history_window(
        messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        from services.runtime_config import get_ai_operations_setting

        window = get_ai_operations_setting("history_window", 20)
        if window <= 0:
            return messages
        max_msgs = window * 2
        if len(messages) <= max_msgs:
            return messages
        return messages[-max_msgs:]

    # ------------------------------------------------------------------
    # Tool filtering
    # ------------------------------------------------------------------

    @staticmethod
    def filter_tools_by_name(
        tools: List[Dict[str, Any]],
        recommended: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        if not recommended:
            return tools
        wanted = set(recommended)
        out: List[Dict[str, Any]] = []
        for t in tools:
            name = t.get("name", "")
            if name in wanted:
                out.append(t)
                continue
            if "_" in name and name.split("_", 1)[1] in wanted:
                out.append(t)
        return out

    # ------------------------------------------------------------------
    # Prompt caching (Anthropic cache_control blocks)
    # ------------------------------------------------------------------

    @staticmethod
    def apply_prompt_cache_controls(api_kwargs: Dict[str, Any]) -> None:
        from services.runtime_config import get_ai_operations_setting

        if not get_ai_operations_setting("prompt_cache_enabled", True):
            return

        system = api_kwargs.get("system")
        if isinstance(system, str) and system:
            api_kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(system, list) and system:
            last = system[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                system[-1] = {**last, "cache_control": {"type": "ephemeral"}}

        tools = api_kwargs.get("tools")
        if isinstance(tools, list) and tools:
            last_tool = tools[-1]
            if isinstance(last_tool, dict) and "cache_control" not in last_tool:
                api_kwargs["tools"] = tools[:-1] + [
                    {**last_tool, "cache_control": {"type": "ephemeral"}}
                ]

    # ------------------------------------------------------------------
    # Tool response budgets
    # ------------------------------------------------------------------

    @classmethod
    def response_budget_for(cls, tool_name: Optional[str]) -> int:
        if tool_name:
            if tool_name in cls.TOOL_RESPONSE_BUDGETS:
                return cls.TOOL_RESPONSE_BUDGETS[tool_name]
            if "_" in tool_name:
                bare = tool_name.split("_", 1)[1]
                if bare in cls.TOOL_RESPONSE_BUDGETS:
                    return cls.TOOL_RESPONSE_BUDGETS[bare]
        from services.runtime_config import get_ai_operations_setting

        return get_ai_operations_setting("tool_response_budget_default", 8000)

    @classmethod
    def truncate_tool_response(
        cls,
        content: str,
        tool_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        if max_tokens is None:
            max_tokens = cls.response_budget_for(tool_name)
        estimated = len(content) // 4
        if estimated <= max_tokens:
            return content
        truncated = content[: max_tokens * 4]
        return (
            truncated
            + f"\n\n[TRUNCATED: Response was ~{estimated} tokens, showing first ~{max_tokens}. "
            "Use more specific filters or pagination to see remaining data.]"
        )

    # ------------------------------------------------------------------
    # Context reduction helpers
    # ------------------------------------------------------------------

    def needs_context_reduction(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        backend_tools: Optional[List] = None,
        mcp_tools: Optional[List] = None,
        max_context_tokens: int = 180000,
    ) -> Tuple[bool, int, int]:
        system_tokens = self.estimate_tokens(system_prompt) if system_prompt else 0
        tool_tokens = 0
        if backend_tools:
            tool_tokens += self.estimate_tokens(json.dumps(backend_tools))
        if mcp_tools:
            tool_tokens += self.estimate_tokens(json.dumps(mcp_tools))
        available_tokens = max_context_tokens - system_tokens - tool_tokens
        if available_tokens <= 0:
            available_tokens = 50000
        total_tokens = sum(
            self.estimate_tokens(msg.get("content", "")) for msg in messages
        )
        return total_tokens > available_tokens, total_tokens, available_tokens

    @staticmethod
    def split_messages_for_summary(
        messages: List[Dict], available_tokens: int
    ) -> Tuple[List, List]:
        if not messages:
            return [], []
        recent_budget = int(available_tokens * 0.6)
        keep: List = []
        used = 0
        for msg in reversed(messages):
            msg_tokens = ContextManager.estimate_tokens(msg.get("content", ""))
            if used + msg_tokens > recent_budget and len(keep) >= 2:
                break
            keep.insert(0, msg)
            used += msg_tokens
        keep_start_idx = len(messages) - len(keep)
        return messages[:keep_start_idx], keep

    @staticmethod
    def format_messages_for_summary(messages: List[Dict]) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            text = _flatten_content_to_text(msg.get("content", ""))
            if text.strip():
                parts.append(f"{role}: {text}")
        return "\n\n".join(parts)

    @staticmethod
    def build_summary_prompt(conversation_text: str) -> str:
        max_chars = 400000
        if len(conversation_text) > max_chars:
            conversation_text = (
                conversation_text[:max_chars] + "\n\n[... earlier messages truncated ...]"
            )
        return (
            "Summarize the following conversation between a user and an AI security assistant.\n"
            "Preserve ALL finding IDs, case IDs, IOCs, investigation decisions, and entity references.\n\n"
            f"CONVERSATION ({len(conversation_text)} chars):\n"
            f"{conversation_text}\n\n"
            "Provide a structured summary preserving all critical context."
        )

    # ------------------------------------------------------------------
    # Rolling summary compression (no LLM call, provider-agnostic)
    # ------------------------------------------------------------------

    @staticmethod
    def fold_overflow(overflow_messages: List[Dict], existing_summary: str = "") -> str:
        """Fold aged-out messages into a running summary string.

        Pure function — no I/O, no LLM calls. Extracts SOC-relevant entities
        via regex and appends a structured fold block to the existing summary.
        Caps total summary length to avoid unbounded growth.
        """
        if not overflow_messages:
            return existing_summary

        all_text = " ".join(
            _flatten_content_to_text(m.get("content", "")) for m in overflow_messages
        )

        finding_ids = sorted(set(re.findall(r"f-[0-9a-f]{8}-[0-9a-f]{8}", all_text, re.I)))[:10]
        case_ids = sorted(set(re.findall(r"case-[0-9a-f]{8,}", all_text, re.I)))[:10]
        ips = sorted(set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", all_text)))[:10]
        hashes = list(set(re.findall(r"\b[0-9a-f]{40,64}\b", all_text, re.I)))[:5]
        cves = sorted(set(re.findall(r"CVE-\d{4}-\d{4,}", all_text, re.I)))[:10]
        # First short snippet from each assistant turn (investigation analysis)
        assistant_snippets = [
            _flatten_content_to_text(m.get("content", ""))[:150].strip()
            for m in overflow_messages
            if m.get("role") == "assistant"
        ]
        assistant_snippets = [s for s in assistant_snippets if s][:3]

        fold_parts = [f"[{len(overflow_messages)} earlier messages]"]
        if finding_ids:
            fold_parts.append(f"Findings: {', '.join(finding_ids)}")
        if case_ids:
            fold_parts.append(f"Cases: {', '.join(case_ids)}")
        if ips:
            fold_parts.append(f"IPs: {', '.join(ips)}")
        if hashes:
            fold_parts.append(f"Hashes: {', '.join(hashes)}")
        if cves:
            fold_parts.append(f"CVEs: {', '.join(cves)}")
        if assistant_snippets:
            fold_parts.append("Analysis: " + " | ".join(assistant_snippets))

        fold_text = "; ".join(fold_parts)
        combined = (existing_summary + "\n" + fold_text).lstrip("\n") if existing_summary else fold_text

        # Cap to avoid unbounded growth; keep the tail (most recent folds).
        if len(combined) > _SUMMARY_MAX_CHARS:
            combined = combined[-_SUMMARY_MAX_CHARS:]
            nl = combined.find("\n")
            if nl > 0:
                combined = "[...earlier context truncated...]\n" + combined[nl + 1:]

        return combined

    # ------------------------------------------------------------------
    # Main context preparation (replaces prepare_context_sync/async)
    # ------------------------------------------------------------------

    def prepare_context(
        self,
        messages: List[Dict],
        summary: str = "",
        system_prompt: Optional[str] = None,
        backend_tools: Optional[List] = None,
        mcp_tools: Optional[List] = None,
        max_context_tokens: int = 180000,
    ) -> Tuple[List[Dict], List[Dict]]:
        """Apply sliding window and prepend rolling summary. Zero LLM calls.

        Returns:
            (prepared_messages, overflow_messages)

        ``overflow_messages`` are the messages that aged out of the window.
        Callers should fold them into the session summary asynchronously.
        """
        from services.runtime_config import get_ai_operations_setting

        window = get_ai_operations_setting("history_window", 20)
        max_msgs = window * 2 if window > 0 else len(messages)

        if len(messages) > max_msgs:
            overflow = list(messages[:-max_msgs])
            windowed = list(messages[-max_msgs:])
        else:
            overflow = []
            windowed = list(messages)

        # Fold the overflow into the summary *for this request* rather than only
        # persisting it for a future one. Each per-request ClaudeService starts
        # with an empty in-memory summary (nothing hydrates it from disk), so if
        # we prepended only ``summary`` here the aged-out messages would be
        # silently dropped instead of compressed. ``fold_overflow`` is a pure,
        # bounded function, so re-folding from the original ``summary`` each pass
        # of the trim loop stays correct (no double-counting) and cheap.
        folded = self.fold_overflow(overflow, summary) if overflow else summary
        prepared = _prepend_summary_block(folded, windowed)

        # Safety: if still over budget (e.g. very large tool outputs), hard-trim
        # from the front of the window without blocking.
        needs, _, _ = self.needs_context_reduction(
            prepared, system_prompt, backend_tools, mcp_tools, max_context_tokens
        )
        while needs and len(windowed) > 2:
            overflow.append(windowed[0])
            windowed = windowed[1:]
            folded = self.fold_overflow(overflow, summary)
            prepared = _prepend_summary_block(folded, windowed)
            needs, _, _ = self.needs_context_reduction(
                prepared, system_prompt, backend_tools, mcp_tools, max_context_tokens
            )

        if needs:
            logger.warning(
                "Context still over budget after window trim; using remaining %d messages",
                len(windowed),
            )

        return prepared, overflow

    # Back-compat: thin wrappers so any direct callers of the old sync/async
    # methods still work. Both are now synchronous and ignore `model`.
    def prepare_context_sync(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_context_tokens: int = 180000,
        backend_tools: Optional[List] = None,
        mcp_tools: Optional[List] = None,
    ) -> Tuple[List[Dict], int]:
        prepared, overflow = self.prepare_context(
            messages, "", system_prompt, backend_tools, mcp_tools, max_context_tokens
        )
        return prepared, len(overflow)

    async def prepare_context_async(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_context_tokens: int = 180000,
        backend_tools: Optional[List] = None,
        mcp_tools: Optional[List] = None,
    ) -> Tuple[List[Dict], int]:
        prepared, overflow = self.prepare_context(
            messages, "", system_prompt, backend_tools, mcp_tools, max_context_tokens
        )
        return prepared, len(overflow)
