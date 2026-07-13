"""Provider-aware text generation for service-layer callers.

This module is the narrow bridge between older service code that expects a
plain text completion and the multi-provider registry/router stack. It keeps
Ollama/OpenAI-only installs from falling through to ``ClaudeService`` while
preserving the existing Anthropic tool-capable path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.defaults import DEFAULT_MODEL

logger = logging.getLogger(__name__)


NO_PROVIDER_CONFIGURED = {
    "code": "no_llm_provider_configured",
    "message": (
        "No LLM provider is configured. Add one in Settings -> AI / LLM "
        "Providers, then try again."
    ),
    "settings_path": "/settings#llm-providers",
}


@dataclass(frozen=True)
class ConfiguredLLMSelection:
    """Resolved provider/model pair for a component."""

    provider_id: Optional[str]
    provider_type: str
    model: str
    provider: Any = None


@dataclass(frozen=True)
class ConfiguredTextResult:
    """Plain text completion plus routing metadata."""

    content: str
    provider_id: Optional[str]
    provider_type: str
    model: str
    path: str
    raw: Dict[str, Any]


class NoConfiguredLLMProvider(RuntimeError):
    """Raised when the selected provider cannot serve a request."""

    def __init__(self, detail: Dict[str, str] | str = NO_PROVIDER_CONFIGURED):
        self.detail = detail
        message = detail.get("message", str(detail)) if isinstance(detail, dict) else detail
        super().__init__(message)


def _model_for_provider(provider: Any, requested_model: Optional[str]) -> str:
    """Return a model valid for ``provider``.

    A stale Claude model in ``ai_model_configs`` must not be sent to Ollama or
    OpenAI. Match the chat endpoint's behavior and pin non-Anthropic providers
    back to their own default model.
    """
    model = requested_model or getattr(provider, "default_model", None) or DEFAULT_MODEL
    if model.startswith("claude-") and getattr(provider, "provider_type", None) != "anthropic":
        return provider.default_model
    return model


def resolve_configured_llm(component: str = "chat_default") -> ConfiguredLLMSelection:
    """Resolve the effective provider/model for a component."""
    from services.llm_router import get_default_provider_spec, get_provider_spec
    from services.model_registry import get_registry

    resolved = get_registry().resolve_model_for_component(component)
    provider_id, model = resolved if resolved is not None else (None, DEFAULT_MODEL)

    provider = None
    if provider_id:
        try:
            provider = get_provider_spec(provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("provider lookup failed for %s: %s", provider_id, exc)
            provider = None
    if provider is None:
        try:
            provider = get_default_provider_spec()
        except Exception as exc:  # noqa: BLE001
            logger.debug("default provider lookup failed: %s", exc)
            provider = None

    if provider is None:
        return ConfiguredLLMSelection(
            provider_id=provider_id,
            provider_type="anthropic",
            model=model,
            provider=None,
        )

    return ConfiguredLLMSelection(
        provider_id=getattr(provider, "provider_id", provider_id),
        provider_type=getattr(provider, "provider_type", "anthropic"),
        model=_model_for_provider(provider, model),
        provider=provider,
    )


def _messages_from_context(
    message: str, context: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in context or []:
        role = item.get("role") or "user"
        content = item.get("content") or ""
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


def _non_anthropic_system_prompt(
    system_prompt: Optional[str],
    recommended_tools: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    if not recommended_tools:
        return system_prompt
    guardrail = (
        "This provider path cannot execute tools in this service call. "
        "Do not emit tool calls or claim to have queried external systems; "
        "work only from the supplied prompt and context."
    )
    return f"{system_prompt}\n\n{guardrail}" if system_prompt else guardrail


async def generate_configured_text(
    *,
    message: str,
    component: str = "chat_default",
    context: Optional[List[Dict[str, Any]]] = None,
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    recommended_tools: Optional[List[Dict[str, Any]]] = None,
    use_backend_tools: bool = True,
    use_mcp_tools: bool = False,
    enable_thinking: bool = False,
    thinking_budget: int = 10000,
) -> ConfiguredTextResult:
    """Generate text through the configured component provider."""
    selection = resolve_configured_llm(component)

    if selection.provider is not None and selection.provider_type != "anthropic":
        from services.llm_router import LLMRouter

        result = await LLMRouter().dispatch(
            provider=selection.provider,
            messages=_messages_from_context(message, context),
            system_prompt=_non_anthropic_system_prompt(
                system_prompt, recommended_tools
            ),
            model=selection.model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=None,
            enable_thinking=False,
        )
        return ConfiguredTextResult(
            content=result.get("content", ""),
            provider_id=selection.provider_id,
            provider_type=selection.provider_type,
            model=selection.model,
            path=result.get("path", "bifrost"),
            raw=result,
        )

    from services.claude_service import ClaudeService

    claude_service = ClaudeService(
        use_backend_tools=use_backend_tools,
        use_mcp_tools=use_mcp_tools,
        use_agent_sdk=False,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
    )
    if not claude_service.has_api_key():
        raise NoConfiguredLLMProvider()

    chat_kwargs: Dict[str, Any] = {
        "message": message,
        "system_prompt": system_prompt,
        "model": selection.model,
        "max_tokens": max_tokens,
        "recommended_tools": recommended_tools,
    }
    # Preserve the established ClaudeService call shape for callers that do
    # not supply conversation history.  Passing ``context=None`` is redundant
    # and breaks compatible test doubles and adapters with the older keyword
    # signature.  Real context is still forwarded unchanged when present.
    if context is not None:
        chat_kwargs["context"] = context

    response = await asyncio.to_thread(claude_service.chat, **chat_kwargs)
    return ConfiguredTextResult(
        content=response or "",
        provider_id=selection.provider_id,
        provider_type="anthropic",
        model=selection.model,
        path="claude_service",
        raw={"content": response or ""},
    )


async def estimate_configured_cost(
    *,
    component: str,
    message: str,
    system_prompt: Optional[str],
    max_tokens: int,
) -> Optional[Dict[str, Any]]:
    """Best-effort cost estimate for the configured component provider."""
    try:
        from services.cost_estimator import estimate_cost

        selection = resolve_configured_llm(component)
        estimate = await estimate_cost(
            provider_type=selection.provider_type,
            model_id=selection.model,
            messages=[{"role": "user", "content": message}],
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        return estimate.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("configured cost estimate failed: %s", exc)
        return None
