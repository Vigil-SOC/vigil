"""One-shot completions on whichever provider Settings assigns.

For drafting features (workflow, agent and integration generators) that only
need text back. They used to build a ``ClaudeService`` and refuse to run
without an Anthropic key, so an Ollama- or OpenAI-only install could not use
them. This resolves the component's assignment with ``resolve_dispatch`` and
sends the call through ``LLMRouter`` like condensing and hunt claims do.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

NOT_CONFIGURED = "No AI provider is configured. Add one in Settings → AI."


class NoProviderConfigured(Exception):
    """Nothing is assigned to the component and no default provider is active."""


async def complete(
    *,
    message: str,
    system_prompt: Optional[str] = None,
    context: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 4096,
    component: str = "chat_default",
) -> str:
    """The model's text reply. Raises ``NoProviderConfigured`` when nothing can
    serve ``component``; dispatch errors propagate to the caller."""
    from core.llm.router.router import LLMRouter
    from core.llm.target import resolve_dispatch, resolve_effort

    # The assignment lookup reads the database; keep it off the event loop
    # so the API answers while a draft is written (#3180).
    resolved = await asyncio.to_thread(resolve_dispatch, component)
    if resolved is None:
        raise NoProviderConfigured(NOT_CONFIGURED)
    provider, model = resolved
    effort = await asyncio.to_thread(resolve_effort, component)
    messages = list(context or []) + [{"role": "user", "content": message}]
    result = await LLMRouter().dispatch(
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        messages=messages,
        max_tokens=max_tokens,
        effort=effort,
    )
    return (result.get("content") or "").strip()
