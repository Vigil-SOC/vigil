"""One-shot Anthropic completions. Tools and the loop live in the agent layer."""

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from core.llm.defaults import DEFAULT_MODEL
from core.secrets import get_secret

try:
    # Anthropic imports are retained for type references and the Bifrost-routed
    # client helpers imported just below. Direct construction happens through
    # `create_anthropic_client` / `create_async_anthropic_client` in
    # core.llm.providers.clients so every Anthropic call flows through Bifrost (GH #84).
    from anthropic import Anthropic, AsyncAnthropic  # noqa: F401

    from core.llm.providers.clients import (
        create_anthropic_client,
        create_async_anthropic_client,
    )

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

logger = logging.getLogger(__name__)


class ClaudeService:
    """One completion at a time. Callers that need a system prompt pass one."""

    def __init__(self, provider_api_key_ref: Optional[str] = None):
        """Args:
        provider_api_key_ref: Optional secret-manager key for a non-default
            Anthropic provider row (GH #88). When set, _load_api_key reads
            this secret first before the legacy CLAUDE_API_KEY fallback chain.
        """
        self.client: Optional[Anthropic] = None
        self.async_client: Optional[AsyncAnthropic] = None
        self.api_key: Optional[str] = None
        self.provider_api_key_ref = provider_api_key_ref
        self._load_api_key()

    def _load_api_key(self) -> bool:
        """Load API key from secure storage.

        Resolution order:

        1. ``provider_api_key_ref`` when explicitly passed at init (GH #88).
        2. Legacy ``CLAUDE_API_KEY`` / ``ANTHROPIC_API_KEY`` env / secret names.
        3. UI-saved Anthropic provider rows in ``llm_provider_configs``.

        Step 3 was the missing piece behind the "Claude API not configured"
        chat-drawer error reported when users configured Anthropic only
        through Settings → AI / LLM Providers: that path writes the key to
        ``llm_provider_<id>_api_key`` (see ``services/api/routers/llm_providers.py``)
        — not to the legacy names this method used to check.
        """
        try:
            # Use secrets manager with fallback to legacy names
            provider_key = (
                get_secret(self.provider_api_key_ref)
                if self.provider_api_key_ref
                else None
            )
            self.api_key = (
                provider_key
                or get_secret("CLAUDE_API_KEY")
                or get_secret("ANTHROPIC_API_KEY")
                or get_secret("claude_api_key")
                or get_secret("anthropic_api_key")
            )

            # Fallback: pick up keys saved by the LLM Providers UI. Lazy
            # import keeps the legacy/no-DB code path (and the unit tests
            # that pre-date this fallback) working when core.storage.connection
            # isn't importable.
            if not self.api_key:
                try:
                    from core.llm.router.router import discover_anthropic_api_key

                    self.api_key = discover_anthropic_api_key()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("UI-provider key discovery skipped: %s", exc)

            if self.api_key and ANTHROPIC_AVAILABLE:
                # Set longer timeout for operations that may take more than 10 minutes
                # Default is 600 seconds (10 min), we set to 1800 seconds (30 min)
                self.client = create_anthropic_client(self.api_key, timeout=1800.0)
                self.async_client = create_async_anthropic_client(
                    self.api_key, timeout=1800.0
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error loading API key: {e}")
            return False

    def has_api_key(self) -> bool:
        """Return True if this ClaudeService can call the Anthropic SDK.

        Deliberately Anthropic-specific: every caller that gates on this
        method goes on to invoke ``self.client`` / ``self.async_client``
        (the Anthropic SDK). Reporting True for a non-Anthropic provider
        would let those callers through and then crash with AttributeError
        when ``self.client`` is None on an Ollama/OpenAI-only deployment.

        Non-Anthropic routing is handled separately by the chat endpoints
        in ``services/api/routers/claude.py``, which resolve the active provider via
        ``get_default_provider_spec()`` and dispatch through ``LLMRouter``
        without ever touching ClaudeService.
        """
        return self.api_key is not None and self.client is not None

    def _extract_content_blocks(self, content) -> Union[str, List[Dict]]:
        """Text blocks from a one-shot response. A single block is a string."""
        blocks = []
        for content_block in content or []:
            if getattr(content_block, "type", None) == "text" and hasattr(
                content_block, "text"
            ):
                blocks.append({"type": "text", "text": content_block.text})
        if len(blocks) == 1:
            return blocks[0]["text"]
        return blocks or None

    # ------------------------------------------------------------------
    # Reasoning-trace persistence (GH #79)
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_response_blocks(content) -> List[Dict]:
        """Convert Anthropic SDK content blocks to JSON-safe dicts."""
        if not content:
            return []
        out = []
        for block in content:
            btype = (
                getattr(block, "type", None)
                if not isinstance(block, dict)
                else block.get("type")
            )
            if btype == "text":
                text = (
                    block.text if not isinstance(block, dict) else block.get("text", "")
                )
                out.append({"type": "text", "text": text})
            elif btype == "thinking":
                text = (
                    block.thinking
                    if not isinstance(block, dict)
                    else block.get("text") or block.get("thinking", "")
                )
                out.append({"type": "thinking", "text": text})
            elif btype == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": (
                            getattr(block, "id", None)
                            if not isinstance(block, dict)
                            else block.get("id")
                        ),
                        "name": (
                            getattr(block, "name", None)
                            if not isinstance(block, dict)
                            else block.get("name")
                        ),
                        "input": (
                            getattr(block, "input", None)
                            if not isinstance(block, dict)
                            else block.get("input")
                        ),
                    }
                )
            elif btype == "tool_result":
                out.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": (
                            getattr(block, "tool_use_id", None)
                            if not isinstance(block, dict)
                            else block.get("tool_use_id")
                        ),
                        "content": (
                            getattr(block, "content", None)
                            if not isinstance(block, dict)
                            else block.get("content")
                        ),
                        "is_error": (
                            getattr(block, "is_error", False)
                            if not isinstance(block, dict)
                            else block.get("is_error", False)
                        ),
                    }
                )
        return out

    @staticmethod
    def _sanitize_messages_for_log(messages: List[Dict]) -> List[Dict]:
        """Strip heavy image base64 payloads from messages before logging."""
        if not messages:
            return []
        sanitized = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, str):
                sanitized.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                sanitized.append({"role": role, "content": content})
                continue
            clean_blocks = []
            for block in content:
                bdict = (
                    block
                    if isinstance(block, dict)
                    else {"type": getattr(block, "type", "unknown")}
                )
                btype = bdict.get("type")
                if btype == "image":
                    clean_blocks.append(
                        {"type": "image", "source": {"type": "redacted"}}
                    )
                else:
                    clean_blocks.append(
                        ClaudeService._serialize_response_blocks([block])[0]
                        if not isinstance(block, dict)
                        else block
                    )
            sanitized.append({"role": role, "content": clean_blocks})
        return sanitized

    @staticmethod
    def _extract_prior_tool_results(messages: List[Dict]) -> List[Dict]:
        """Return tool_result blocks from the most recent user message, if any.

        Used to capture the "input" context for an iteration that consumed
        tool results from the prior iteration's tool calls.
        """
        if not messages:
            return []
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                results = [
                    b
                    for b in content
                    if (isinstance(b, dict) and b.get("type") == "tool_result")
                    or (
                        not isinstance(b, dict)
                        and getattr(b, "type", None) == "tool_result"
                    )
                ]
                if results:
                    return ClaudeService._serialize_response_blocks(results)
            return []
        return []

    def _persist_interaction(
        self,
        *,
        session_id: Optional[str],
        agent_id: Optional[str],
        investigation_id: Optional[str],
        model: str,
        system_prompt: Optional[str],
        request_messages: List[Dict],
        response_content: Optional[List[Dict]],
        thinking_enabled: bool,
        thinking_budget: Optional[int],
        stop_reason: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_ms: int = 0,
        error: Optional[str] = None,
        interaction_id: Optional[str] = None,
    ) -> None:
        """Fire-and-forget insert of an LLMInteractionLog row.

        Runs in the calling thread; failures are logged but never re-raised
        so persistence can never break the request path.
        """
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import LLMInteractionLog

            blocks = self._serialize_response_blocks(response_content or [])
            thinking_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "thinking"
            )
            response_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "text"
            )
            tool_calls = [b for b in blocks if b["type"] == "tool_use"]
            tool_results_in = self._extract_prior_tool_results(request_messages)

            try:
                # GH #89: use the model registry for per-provider pricing.
                # #184 Phase 3: include cache tokens so reads (0.1×) and
                # writes (1.25×) are priced correctly instead of being
                # treated as full-rate input.
                from core.llm.cost.calls import compute_call_cost

                cost_usd = compute_call_cost(
                    model,
                    "anthropic",
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    cache_read_tokens=int(cache_read_tokens or 0),
                    cache_creation_tokens=int(cache_creation_tokens or 0),
                )
            except Exception:
                cost_usd = 0.0

            # #186: capture which Bifrost VK serviced this call so we can
            # group spend per-VK in analytics. Empty in dev / bypass mode.
            try:
                from core.llm.cost.budget import get_active_vk

                _vk = get_active_vk()
            except Exception:
                _vk = None

            row = LLMInteractionLog(
                # Caller-supplied interaction_id (#185 Bifrost correlation)
                # falls back to a fresh UUID for legacy callers that don't
                # generate it upstream of the dispatch.
                interaction_id=interaction_id or str(uuid.uuid4()),
                session_id=session_id,
                agent_id=agent_id,
                investigation_id=investigation_id,
                model=model,
                system_prompt=system_prompt,
                request_messages=self._sanitize_messages_for_log(request_messages),
                thinking_enabled=bool(thinking_enabled),
                thinking_budget=thinking_budget,
                thinking_content=thinking_text or None,
                response_content=response_text or None,
                tool_calls=tool_calls,
                tool_results=tool_results_in,
                stop_reason=stop_reason,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_creation_tokens=int(cache_creation_tokens or 0),
                cost_usd=float(cost_usd or 0.0),
                duration_ms=int(duration_ms or 0),
                error=error,
                virtual_key_id=_vk,
            )
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                session.add(row)
        except Exception as exc:
            logger.warning(f"LLMInteractionLog persist failed (non-fatal): {exc}")

    def chat(
        self,
        message: Union[str, List[Dict]],
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict]] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        investigation_id: Optional[str] = None,
    ) -> Optional[str]:
        """One completion. No tools and no loop -- both live in the agent layer."""
        if not self.has_api_key():
            logger.error("No API key configured")
            return None

        messages = list(context or []) + [{"role": "user", "content": message}]
        api_kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            # Correlates the Bifrost LogEntry with the row persisted below.
            "extra_headers": {"x-bf-lh-vigil-interaction-id": str(uuid.uuid4())},
        }
        if system_prompt:
            api_kwargs["system"] = system_prompt

        started = time.monotonic()
        try:
            response = self.client.messages.create(**api_kwargs)
        except Exception as exc:
            logger.error(f"Error in Claude chat: {exc}")
            raise

        usage = getattr(response, "usage", None)
        self._persist_interaction(
            session_id=session_id,
            agent_id=agent_id,
            investigation_id=investigation_id,
            model=getattr(response, "model", model),
            system_prompt=system_prompt,
            request_messages=messages,
            response_content=list(response.content) if response.content else [],
            thinking_enabled=False,
            thinking_budget=None,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        extracted = self._extract_content_blocks(response.content)
        return extracted if isinstance(extracted, str) else json.dumps(extracted)
