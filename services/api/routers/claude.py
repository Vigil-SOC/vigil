"""Claude endpoints: model listing and the chat stream the console uses."""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from core.agents.projections import agent_route
from core.deps import provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry, live_mcp_tools
from core.llm.chat_layers import chat_config, run_id_for
from core.llm.defaults import DEFAULT_MODEL
from core.llm.providers.registry import get_registry
from core.llm.system_prompt import validate_system_prompt
from core.llm.target import model_for, provider_for
from core.rate_limit import rate_limit_dependency
from core.routing import Auth, RouterMeta
from core.secrets import get_secret
from core.storage.models import User
from services.api.middleware.auth import get_current_user

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/claude",
    tags=["claude"],
    # These routes expose AI and agent execution, so they must require an
    # authenticated session AND keep rate limiting on top of it — the cost of an
    # unmetered call here is real money, not just data exposure.
    auth=Auth.REQUIRED,
    extra_dependencies=(Depends(rate_limit_dependency),),
)


def _user_text_from_content(content) -> str:
    """Best-effort plain text from a chat message's content (str or blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def _persist_chat_turn(
    *,
    session_id: str,
    user_id: Optional[str],
    agent_id: Optional[str],
    model: Optional[str],
    user_text: str,
    assistant_text: str,
    assistant_thinking: Optional[str],
    tool_calls: list,
    complete: bool,
) -> None:
    """Fail-open write-through of one chat turn to the conversation store.

    Sync — invoked via ``asyncio.to_thread`` from the SSE generator. Persists
    the user turn (always complete) and the assistant turn (``complete=False``
    on abort/error). Each ``conversation_service`` call is itself fail-open;
    this wrapper adds a final guard so nothing here can surface into the
    request path. The authoritative per-iteration record (including full tool
    calls / results) remains in ``llm_interaction_logs`` keyed by the same
    ``session_id``.
    """
    try:
        from core.chat import conversation_service

        conversation_service.ensure_conversation(
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            model=model,
            first_user_text=user_text,
        )
        conversation_service.append_message(
            session_id=session_id,
            role="user",
            content=user_text,
            complete=True,
        )
        conversation_service.append_message(
            session_id=session_id,
            role="assistant",
            content=assistant_text,
            thinking=assistant_thinking,
            tool_calls=tool_calls,
            complete=complete,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open, never break the chat
        logger.warning("chat history write-through failed (non-fatal): %s", exc)


logger = logging.getLogger(__name__)


# Structured payload returned when no Anthropic provider is configured.
# The chat drawer matches on ``code`` and renders a "Configure a provider"
# CTA instead of a generic ``Error: ...`` bubble.
NO_PROVIDER_DETAIL: Dict[str, str] = {
    "code": "no_llm_provider_configured",
    "message": (
        "No LLM provider is configured. "
        "Add one in Settings → AI / LLM Providers, then try again."
    ),
    "settings_path": "/settings#llm-providers",
}


def _raise_no_provider() -> None:
    """Raise the canonical 503 for an unconfigured chat backend."""
    raise HTTPException(status_code=503, detail=NO_PROVIDER_DETAIL)


def _resolve_provider_model_for_request(
    requested_model: Optional[str], agent_id: Optional[str]
) -> Tuple[Optional[str], str]:
    """Resolve provider + model for chat requests.

    Older Chat UI state can persist model ids as ``provider_id::model_id``.
    Keep accepting that shape so a stale localStorage value does not route an
    Ollama/OpenAI model through the wrong provider.
    """
    if requested_model:
        if "::" in requested_model:
            provider_id, model_id = requested_model.split("::", 1)
            return (provider_id or None, model_id or requested_model)
        return (None, requested_model)

    registry = get_registry()
    agent_override: Optional[str] = None
    category: str = "chat_default"

    if agent_id:
        try:
            from core.agents.manager import AgentManager

            agent = AgentManager().agents.get(agent_id)
            if agent is not None:
                agent_override = getattr(agent, "model", None)
                category = getattr(agent, "component_category", None) or "investigation"
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent lookup in model resolution failed: %s", exc)

    if agent_override:
        resolved = registry.resolve_model_for_component(
            category, agent_override=agent_override
        )
    else:
        resolved = registry.resolve_model_for_component(category)

    if resolved is not None:
        return resolved
    # Hard fallback (no provider/registry hit) — centralised so Ollama-only
    # deployments can override via DEFAULT_MODEL instead of hardcoding Claude.
    return (None, DEFAULT_MODEL)


class ContentBlock(BaseModel):
    """Content block for message (text or image)."""

    type: str  # "text" or "image"
    text: Optional[str] = None
    source: Optional[Dict[str, Any]] = (
        None  # For image: {"type": "base64", "media_type": "...", "data": "..."}
    )


class ChatMessage(BaseModel):
    """Chat message model."""

    role: str  # user or assistant
    content: Union[str, List[ContentBlock]]  # Can be string or list of content blocks

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: Union[str, List]) -> Union[str, List]:
        if isinstance(v, str) and not v.strip():
            raise ValueError("message content must not be empty")
        return v


class ChatRequest(BaseModel):
    """Chat request model."""

    messages: List[ChatMessage]
    system_prompt: Optional[str] = None
    # None means "resolve via ai_model_configs" (GH #89). Callers may still
    # override with an explicit model id.
    model: Optional[str] = None
    max_tokens: int = 4096
    agent_id: Optional[str] = None
    session_id: Optional[str] = (
        None  # Chat session identifier for reasoning-trace persistence (GH #79)
    )
    # A run this conversation follows up on. The console does not send one yet
    # (#634); when it does, the turn opens with what that run concluded.
    parent_run_id: Optional[str] = None

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: Optional[str]) -> Optional[str]:
        return validate_system_prompt(v, source="chat")


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """Stream a chat turn from the agent layer, holding this wire contract."""
    # Resolved here because this is the side that knows what an agent is: the
    # harness is handed a prompt, a model and a tool list, never an agent id.
    provider_id, resolved_model = _resolve_provider_model_for_request(
        request.model, request.agent_id
    )
    request.model = resolved_model

    system_prompt = request.system_prompt
    tools: Optional[List[str]] = None
    if request.agent_id:
        from core.agents.manager import AgentManager

        agent = AgentManager().agents.get(request.agent_id)
        if agent:
            system_prompt = agent.system_prompt
            tools = list(agent.recommended_tools) if agent.recommended_tools else None

    active_provider = provider_for(provider_id)
    if active_provider is None:
        _raise_no_provider()
    request.model = model_for(active_provider, request.model)

    # Surface whatever MCP integrations are connected right now (VirusTotal, OTX,
    # MISP, Shodan, …) so the assistant can call them the moment their server is
    # connected — refreshed per turn, no restart.
    mcp_tools = live_mcp_tools(registry) or None

    session_id = request.session_id or str(uuid.uuid4())
    payload = {
        "run_id": run_id_for(session_id),
        "turns": _turns_of(request.messages),
        "system_prompt": system_prompt or "",
        # The provider rides alongside the model so the gateway routes to the
        # account this request resolved to, rather than to whichever provider
        # claims the bare model name first.
        "config": chat_config(
            request.model, tools, mcp_tools, provider=active_provider.provider_type
        ),
    }
    if request.parent_run_id:
        payload["parent_run_id"] = request.parent_run_id

    if not payload["turns"]:
        raise HTTPException(status_code=400, detail="No messages provided")

    return StreamingResponse(
        _relay(payload, request, session_id, getattr(current_user, "user_id", None)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# Images and thinking blocks are dropped: one provider schema through Bifrost
# carries neither, and a block silently reshaped is worse than one left out.
def _turns_of(messages: List[ChatMessage]) -> List[Dict[str, str]]:
    turns: List[Dict[str, str]] = []
    for message in messages:
        text = _user_text_from_content(message.content).strip()
        if not text:
            continue
        role = "assistant" if message.role == "assistant" else "user"
        # Consecutive same-role turns are merged rather than sent as two: the
        # agent layer folds history and a doubled role reads as a lost turn.
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] = turns[-1]["content"] + "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    return turns


# Relayed rather than re-encoded: the agent layer already speaks the console's
# vocabulary, so this reads the frames only to accumulate the turn for history.
async def _relay(
    payload: Dict[str, Any],
    request: ChatRequest,
    session_id: str,
    user_id: Optional[str],
):
    import httpx

    said: List[str] = []
    finished = False
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                agent_route("/chat/stream"),
                json=payload,
                headers=_internal_headers(),
            ) as upstream:
                if upstream.status_code != 200:
                    detail = (await upstream.aread()).decode("utf-8", "replace")
                    yield _frame({"error": f"agent layer refused the turn: {detail}"})
                    return
                async for line in upstream.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    said.append(_text_in(line[6:]))
                    yield f"{line}\n\n"
        finished = True
    except Exception as exc:  # noqa: BLE001 — the reader gets a frame, not a 500
        logger.error("chat stream relay failed: %s", exc, exc_info=True)
        yield _frame({"error": str(exc)})
    finally:
        # Fail-open, and on abort too: GeneratorExit flows through finally.
        try:
            await asyncio.to_thread(
                _persist_chat_turn,
                session_id=session_id,
                user_id=user_id,
                agent_id=request.agent_id,
                model=request.model,
                user_text=payload["turns"][-1]["content"],
                assistant_text="".join(said),
                assistant_thinking=None,
                tool_calls=[],
                complete=finished,
            )
        except Exception as exc:  # noqa: BLE001 — history never breaks the chat
            logger.warning("chat history persist failed (non-fatal): %s", exc)


def _frame(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _text_in(data: str) -> str:
    try:
        event = json.loads(data)
    except ValueError:
        return ""
    return event.get("content", "") if event.get("type") == "text" else ""


def _internal_headers() -> Dict[str, str]:
    token = get_secret("AGENT_INTERNAL_TOKEN") or ""
    if not token:
        raise HTTPException(
            status_code=503, detail="AGENT_INTERNAL_TOKEN is not configured"
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@router.get("/models")
async def get_models():
    """List available models for the Chat UI model picker.

    Backward-compatible alias for `/api/ai/models` (GH #89). Returns every
    model across all *active* providers (Anthropic, Ollama, OpenAI, …) so the
    picker reflects what the instance can actually run: an Ollama-only
    deployment shows its Ollama models instead of Claude models it will never
    call. IDs stay as bare model ids so a persisted `chat_default.model_id`
    selection still matches a menu entry; the chat send path resolves the
    provider from the active default when no `provider_id::` prefix is present
    (see `_resolve_provider_model_for_request`).
    """
    registry = get_registry()
    try:
        all_models = await registry.list_available_models()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_models: registry lookup failed: %s", exc)
        all_models = []

    if not all_models:
        # Live discovery returned nothing. Before rendering anything, reflect
        # the providers the instance actually has configured — an Ollama-only
        # deployment must not be shown Claude models it can't call (#409).
        try:
            all_models = await registry.fallback_models()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_models: provider fallback failed: %s", exc)
            all_models = []

    if not all_models:
        # Genuinely nothing configured (fresh install / no DB). Last-resort
        # default so the Chat UI still renders a picker.
        return {
            "models": [
                {
                    "id": "claude-sonnet-4-5-20250929",
                    "name": "Claude Sonnet 4.5",
                    "description": "Most intelligent model, best for complex tasks",
                },
                {
                    "id": "claude-sonnet-4-6",
                    "name": "Claude Sonnet 4.6",
                    "description": "Balanced speed and intelligence",
                },
                {
                    "id": "claude-haiku-4-5-20251001",
                    "name": "Claude Haiku 4.5",
                    "description": "Fastest model, good for simple tasks",
                },
            ]
        }

    # Dedupe by bare model_id: the picker uses it as both the menu key and the
    # stored value, and two providers can advertise the same id. First wins.
    # Embedding-only models (e.g. nomic-embed-text) are dropped here: they show
    # up in provider discovery but can't hold a chat, so they must not appear in
    # the chat picker. Signal is the registry's is_embedding flag (from the
    # provider capability array), with a name heuristic as fallback for
    # providers/paths that don't carry live capability meta.
    from core.llm.providers.discovery import is_embedding_model_id

    seen: set = set()
    models = []
    for m in all_models:
        if getattr(m, "is_embedding", False) or is_embedding_model_id(m.model_id):
            continue
        if m.model_id in seen:
            continue
        seen.add(m.model_id)
        models.append(
            {
                "id": m.model_id,
                "name": m.display_name,
                "description": (
                    f"{m.context_window // 1000}K context, "
                    f"${m.input_cost_per_1k:.4f}/1K in / "
                    f"${m.output_cost_per_1k:.4f}/1K out"
                ),
            }
        )
    return {"models": models}
