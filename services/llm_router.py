"""LLM router: decides whether a call goes through Bifrost or the direct SDK.

Vigil's multi-provider support (GH #88) relies on Bifrost as a unified
gateway for OpenAI/Ollama/etc. Anthropic + extended thinking bypasses
Bifrost because extended-thinking and native prompt caching don't
round-trip cleanly through Bifrost's OpenAI-format surface today.

Usage::

    from services.llm_router import LLMRouter, DispatchPath

    router = LLMRouter()
    provider = router.resolve_provider(provider_id)   # DB lookup
    path = router.select_path(provider, enable_thinking=True)  # "anthropic_direct"|"bifrost"
    result = await router.dispatch(messages=..., provider=provider, enable_thinking=True)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repo path setup — secrets_manager lives under backend/, which isn't on
# sys.path in the worker/daemon. Mirror the pattern used elsewhere.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:  # soft imports — router is usable in tests without a DB
    from secrets_manager import get_secret  # type: ignore
except Exception:  # noqa: BLE001
    get_secret = None  # type: ignore


DispatchPath = Literal["anthropic_direct", "bifrost"]


@dataclass(frozen=True)
class ProviderSpec:
    """Minimal view of a row from llm_provider_configs.

    Kept as a plain dataclass (not the ORM model) so this module doesn't
    import the SQLAlchemy session into the worker hot path.
    """

    provider_id: str
    provider_type: str
    base_url: Optional[str]
    api_key_ref: Optional[str]
    default_model: str
    config: Dict[str, Any]


def _bifrost_url() -> str:
    return os.getenv("BIFROST_URL", "http://bifrost:8080").rstrip("/")


def select_path(
    provider: ProviderSpec, *, enable_thinking: bool = False
) -> DispatchPath:
    """Decide which dispatch path a request should take.

    Rules (see docker/bifrost/README.md):
      - anthropic + thinking → direct SDK (extended thinking isn't routed)
      - everything else → Bifrost
    """
    if provider.provider_type == "anthropic" and enable_thinking:
        return "anthropic_direct"
    return "bifrost"


class LLMRouter:
    """Thin router that dispatches to Bifrost (openai SDK) or direct Anthropic.

    The router does NOT own the DB session or Anthropic client. Callers
    construct a ProviderSpec from an `LLMProviderConfig` row (e.g. via
    ``provider_spec_from_row``) and pass it in. This keeps the worker hot
    path free of DB imports and makes unit-testing trivial.
    """

    def __init__(self, bifrost_url: Optional[str] = None):
        self.bifrost_url = (bifrost_url or _bifrost_url()).rstrip("/")

    # ---- path selection (pure) -------------------------------------------

    @staticmethod
    def select_path(
        provider: ProviderSpec, *, enable_thinking: bool = False
    ) -> DispatchPath:
        return select_path(provider, enable_thinking=enable_thinking)

    # ---- dispatch --------------------------------------------------------

    async def dispatch(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: bool = False,
        thinking_budget: int = 10000,
    ) -> Dict[str, Any]:
        """Send a chat completion via the appropriate path.

        Returns a dict with at least ``content``, ``model``, ``input_tokens``,
        ``output_tokens``, ``provider``, ``path``.
        """
        path = self.select_path(provider, enable_thinking=enable_thinking)
        model = model or provider.default_model

        if path == "anthropic_direct":
            return await self._dispatch_anthropic(
                provider=provider,
                messages=messages,
                system_prompt=system_prompt,
                model=model,
                max_tokens=max_tokens,
                tools=tools,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
            )
        return await self._dispatch_bifrost(
            provider=provider,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )

    # ---- backends --------------------------------------------------------

    async def _dispatch_bifrost(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        max_tokens: int,
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        from openai import AsyncOpenAI  # lazy — avoids hard dep for tests

        oai_messages: List[Dict[str, Any]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        oai_messages.extend(messages)

        client = AsyncOpenAI(
            base_url=f"{self.bifrost_url}/v1",
            api_key="bifrost",  # Bifrost ignores this; per-provider keys are in its config
        )
        kwargs: Dict[str, Any] = {
            "model": f"{provider.provider_type}/{model}",
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools

        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        return {
            "content": choice.content or "",
            "tool_calls": getattr(choice, "tool_calls", None),
            "model": resp.model,
            "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            "provider": provider.provider_type,
            "path": "bifrost",
        }

    async def _dispatch_anthropic(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]],
        enable_thinking: bool,
        thinking_budget: int,
    ) -> Dict[str, Any]:
        from anthropic import AsyncAnthropic  # lazy

        api_key: Optional[str] = None
        if provider.api_key_ref and get_secret is not None:
            api_key = get_secret(provider.api_key_ref)
        if not api_key:
            # Fall back to common env names so local dev still works.
            api_key = (
                os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("CLAUDE_API_KEY")
            )
        if not api_key:
            raise RuntimeError(
                f"Anthropic provider '{provider.provider_id}' has no resolvable API key"
            )

        client = AsyncAnthropic(api_key=api_key, timeout=1800.0)
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools
        if enable_thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

        resp = await client.messages.create(**kwargs)
        # Anthropic returns a list of content blocks (text, thinking, tool_use).
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_uses: List[Dict[str, Any]] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "thinking":
                thinking_parts.append(getattr(block, "thinking", ""))
            elif btype == "tool_use":
                tool_uses.append({
                    "id": getattr(block, "id", None),
                    "name": getattr(block, "name", None),
                    "input": getattr(block, "input", None),
                })

        usage = getattr(resp, "usage", None)
        return {
            "content": "".join(text_parts),
            "thinking": "".join(thinking_parts) or None,
            "tool_calls": tool_uses or None,
            "model": resp.model,
            "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            "provider": provider.provider_type,
            "path": "anthropic_direct",
        }


# ---------------------------------------------------------------------------
# DB-facing helpers — importable without circular deps
# ---------------------------------------------------------------------------


def provider_spec_from_row(row) -> ProviderSpec:
    """Convert an LLMProviderConfig ORM row into a ProviderSpec."""
    return ProviderSpec(
        provider_id=row.provider_id,
        provider_type=row.provider_type,
        base_url=row.base_url,
        api_key_ref=row.api_key_ref,
        default_model=row.default_model,
        config=dict(row.config or {}),
    )


def get_provider_spec(provider_id: Optional[str]) -> Optional[ProviderSpec]:
    """Load a provider by id (or the default Anthropic row if id is None).

    Returns None if the DB is unavailable — callers should fall back to the
    legacy ClaudeService path in that case.
    """
    try:
        from database.connection import get_db_session
        from database.models import LLMProviderConfig
    except Exception as exc:  # noqa: BLE001
        logger.debug("provider spec DB lookup skipped: %s", exc)
        return None

    session = get_db_session()
    try:
        if provider_id:
            row = session.get(LLMProviderConfig, provider_id)
        else:
            row = (
                session.query(LLMProviderConfig)
                .filter(
                    LLMProviderConfig.provider_type == "anthropic",
                    LLMProviderConfig.is_default.is_(True),
                )
                .first()
            )
        if row is None:
            return None
        return provider_spec_from_row(row)
    finally:
        session.close()
