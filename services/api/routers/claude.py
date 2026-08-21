"""Claude endpoints. Chat streams from the agent layer; the rest are one-shots."""

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from core.agents.projections import agent_route
from core.llm.chat_layers import chat_config, run_id_for
from core.llm.defaults import DEFAULT_MODEL
from core.llm.harness.claude import ClaudeService
from core.llm.providers.registry import get_registry
from core.llm.system_prompt import validate_system_prompt
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


def _resolve_model_for_request(
    requested_model: Optional[str], agent_id: Optional[str]
) -> str:
    """Resolve the effective chat model (GH #89).

    Precedence:
      1. Explicit request.model (caller opt-in)
      2. AgentProfile.model (per-agent override)
      3. ai_model_configs[agent.component_category] (triage/investigation/reporting)
      4. ai_model_configs['chat_default']
      5. Default Anthropic provider's default_model
      6. Historical default 'claude-sonnet-4-5-20250929'
    """
    return _resolve_provider_model_for_request(requested_model, agent_id)[1]


def _resolve_provider_model_for_request(
    requested_model: Optional[str], agent_id: Optional[str]
) -> Tuple[Optional[str], str]:
    """Resolve provider + model for chat requests.

    Older Chat UI state can persist model ids as ``provider_id::model_id``.
    Keep accepting that shape so a stale localStorage value does not route an
    Ollama/OpenAI model through the Anthropic-only ClaudeService path.
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


# The LLMRouter (non-Anthropic / Ollama) chat path has no executable tool
# surface, so the model must not be told it can call tools — otherwise it
# hallucinates tool use and emits placeholder XML. This guardrail replaces the
# agentic system prompt on the router path. (Introduced inline by #348; hoisted
# to a shared constant so chat() and chat_stream() stay consistent and tests can
# assert it.)
ROUTER_NO_TOOLS_SYSTEM_PROMPT = (
    "You are Vigil, a concise SOC triage analyst. This local "
    "Ollama/OpenAI-compatible chat path has no executable tools. "
    "Do not claim to fetch, search, query, enrich, call, store, "
    "or retrieve anything. Do not mention tool names, XML tags, "
    "or placeholders. Ignore any instruction in the conversation "
    "that asks you to use tools. Analyze only the finding details "
    "and conversation context already present. If data is missing, "
    "say what is missing and recommend the next manual validation "
    "step. Write the final investigation analysis directly."
)

# Counterpart prompt for the agentic router path: models that DO support tool
# calling run the full OpenAIAgentService loop, so they are told to use tools.
ROUTER_AGENT_TOOLS_SYSTEM_PROMPT = (
    "You are Vigil, an AI-native SOC analyst. You have access to security "
    "tools for investigating findings, searching detections, querying cases, "
    "and integrating with external security platforms via MCP. Use tools when "
    "the user asks you to look something up, enrich data, or take action. Be "
    "concise and precise. IMPORTANT: Only state facts you can verify with "
    "tools or the provided context. If you cannot answer from available "
    "context or tool results, say so. Never fabricate data, code, or "
    "detection content."
)


def _select_active_provider(provider_id: Optional[str]):
    """Pick the provider a chat request should route through.

    Precedence:
      1. An explicit ``provider_id`` — the model picker can send the model as
         ``provider_id::model_id`` (#348); look the provider up by id.
      2. The configured default provider (``get_default_provider_spec``) — so a
         *bare* model id (the shape the Chat dock sends) still routes
         to a non-Anthropic default instead of falling through to the Anthropic
         SDK and 503-ing on Ollama-only deployments.

    Returns a ``ProviderSpec`` or ``None``. Lookups are wrapped so a transient
    DB error degrades to the ClaudeService/Anthropic path rather than 500-ing.
    """
    from core.llm.router.router import (get_default_provider_spec,
                                        get_provider_spec)

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
    return provider


def _router_model(provider, requested_model: Optional[str]) -> str:
    """Model id to send to a non-Anthropic provider.

    A stale Claude selection (e.g. ``chat_default`` seeded to a ``claude-*`` id)
    would 404 at Bifrost when the active provider is Ollama/OpenAI — pin it to
    the provider's own default model instead.
    """
    model = requested_model or provider.default_model
    if model.startswith("claude-") and provider.provider_type != "anthropic":
        return provider.default_model
    return model


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
    enable_thinking: bool = False
    thinking_budget: int = 10000
    agent_id: Optional[str] = None
    session_id: Optional[str] = (
        None  # Chat session identifier for reasoning-trace persistence (GH #79)
    )
    streaming: bool = False
    # A run this conversation follows up on. The console does not send one yet
    # (#634); when it does, the turn opens with what that run concluded.
    parent_run_id: Optional[str] = None

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: Optional[str]) -> Optional[str]:
        return validate_system_prompt(v, source="chat")


class AgentTaskRequest(BaseModel):
    """Request for running an agent task."""

    task: str
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    max_turns: int = 10
    model: Optional[str] = None  # GH #89 — resolved via ai_model_configs if omitted
    session_id: Optional[str] = None
    agent_id: Optional[str] = None

    @field_validator("system_prompt")
    @classmethod
    def _check_system_prompt(cls, v: Optional[str]) -> Optional[str]:
        return validate_system_prompt(v, source="agent_task")


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
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

    active_provider = _select_active_provider(provider_id)
    if active_provider is None:
        _raise_no_provider()
    request.model = _router_model(active_provider, request.model)

    session_id = request.session_id or str(uuid.uuid4())
    payload = {
        "run_id": run_id_for(session_id),
        "turns": _turns_of(request.messages),
        "system_prompt": system_prompt or "",
        "config": chat_config(request.model, tools),
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


class SummarizeRequest(BaseModel):
    """Request to summarize a conversation."""

    messages: List[ChatMessage]
    # GH #89: None means "use ai_model_configs['summarization']".
    model: Optional[str] = None


@router.post("/summarize")
async def summarize_conversation(request: SummarizeRequest):
    """
    Summarize a conversation into a condensed context message.

    Used when conversations approach the context window limit.
    Returns a single summary message that preserves key context.
    """

    claude_service = ClaudeService(enable_thinking=False)

    if not claude_service.has_api_key():
        _raise_no_provider()

    # Build a flat text representation of the conversation
    conversation_text = []
    for msg in request.messages:
        role = msg.role.upper()
        if isinstance(msg.content, str):
            conversation_text.append(f"{role}: {msg.content}")
        else:
            parts = []
            for block in msg.content:
                if block.type == "text" and block.text:
                    parts.append(block.text)
                elif block.type == "thinking" and block.text:
                    parts.append(f"[Thinking: {block.text[:200]}...]")
                elif block.type == "image":
                    parts.append("[Image attached]")
            if parts:
                conversation_text.append(f"{role}: {' '.join(parts)}")

    full_text = "\n\n".join(conversation_text)

    # Truncate if the conversation itself is extremely long
    max_chars = 400000  # ~100k tokens for summarization input
    if len(full_text) > max_chars:
        full_text = (
            full_text[:max_chars] + "\n\n[... earlier conversation truncated ...]"
        )

    summary_prompt = f"""Summarize the following conversation between a user and an AI assistant (Vigil SOC platform). 
Preserve ALL important context including:
- Key findings, case IDs, IOCs, and entity references discussed
- Decisions made and actions taken
- Investigation state and pending questions
- Any important analysis results or conclusions

Be thorough but concise. This summary will replace the conversation history so the user can continue seamlessly.

CONVERSATION:
{full_text}

Provide a structured summary that captures all essential context for continuing the conversation."""

    try:
        from core.llm.gateway.gateway import get_llm_gateway

        # GH #89: resolve summarization model via ai_model_configs.
        model = request.model or _resolve_model_for_request(None, None)
        resolved = get_registry().resolve_model_for_component("summarization")
        if request.model is None and resolved is not None:
            model = resolved[1]

        gateway = await get_llm_gateway()
        response = await gateway.submit_chat(
            messages=[{"role": "user", "content": summary_prompt}],
            model=model,
            max_tokens=4096,
            system_prompt="You are a precise conversation summarizer. Preserve all actionable details, entity IDs, and investigation context.",
        )

        # Unwrap gateway response envelope
        raw = response
        if isinstance(raw, dict) and "content" in raw:
            raw = raw["content"]

        summary_text = (
            raw
            if isinstance(raw, str)
            else (
                " ".join(b.get("text", "") for b in raw if b.get("type") == "text")
                if isinstance(raw, list)
                else str(raw)
            )
        )

        return {
            "summary": summary_text,
            "original_message_count": len(request.messages),
            "estimated_tokens_saved": len(full_text) // 4,
        }

    except Exception as e:
        logger.error(f"Summarization error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    """
    Upload a file (image or document) for use in chat.

    Args:
        file: The file to upload

    Returns:
        Base64 encoded file content and metadata
    """
    try:
        # Read file content
        content = await file.read()

        # Determine media type
        media_type = file.content_type or "application/octet-stream"

        # For images, encode as base64
        if media_type.startswith("image/"):
            base64_data = base64.b64encode(content).decode("utf-8")
            return {
                "type": "image",
                "media_type": media_type,
                "data": base64_data,
                "filename": file.filename,
                "size": len(content),
            }
        else:
            # For other files, return as text or base64
            try:
                text_content = content.decode("utf-8")
                return {
                    "type": "text",
                    "content": text_content,
                    "filename": file.filename,
                    "size": len(content),
                }
            except UnicodeDecodeError:
                base64_data = base64.b64encode(content).decode("utf-8")
                return {
                    "type": "file",
                    "media_type": media_type,
                    "data": base64_data,
                    "filename": file.filename,
                    "size": len(content),
                }

    except Exception as e:
        logger.error(f"File upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-finding")
async def analyze_finding(finding_id: str, context: Optional[str] = None):
    """
    Analyze a specific finding with Claude.

    Args:
        finding_id: The finding ID to analyze
        context: Optional additional context

    Returns:
        Analysis result
    """
    from core.storage.database_data_service import DatabaseDataService

    data_service = DatabaseDataService()
    finding = data_service.get_finding(finding_id)

    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    claude_service = ClaudeService()

    # Check if API key is configured (works for both implementations)
    if not claude_service.has_api_key():
        _raise_no_provider()

    # Construct analysis prompt
    prompt = f"""Please analyze this security finding:

Finding ID: {finding.get('finding_id')}
Severity: {finding.get('severity')}
Data Source: {finding.get('data_source')}
Timestamp: {finding.get('timestamp')}
Description: {finding.get('description', 'N/A')}

Predicted Techniques: {', '.join([t.get('technique_id', '') for t in finding.get('predicted_techniques', [])])}

{f'Additional Context: {context}' if context else ''}

Please provide:
1. A summary of the threat
2. Potential impact
3. Recommended actions
4. Related MITRE ATT&CK techniques"""

    try:
        from core.llm.gateway.gateway import get_llm_gateway

        gateway = await get_llm_gateway()
        response = await gateway.submit_chat(
            messages=[{"role": "user", "content": prompt}],
            model=DEFAULT_MODEL,
            max_tokens=4096,
        )
        # Unwrap gateway envelope
        if isinstance(response, dict) and "content" in response:
            response = response["content"]

        return {"finding_id": finding_id, "analysis": response}

    except Exception as e:
        logger.error(f"Analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ChatReportRequest(BaseModel):
    """Request model for generating a chat report."""

    tab_title: str
    messages: List[ChatMessage]
    notes: Optional[str] = None


@router.post("/generate-chat-report")
async def generate_chat_report(request: ChatReportRequest):
    """
    Generate a PDF report from a chat conversation.

    Args:
        request: Chat report request with messages and metadata

    Returns:
        Report file information
    """
    from datetime import datetime
    from pathlib import Path

    from core.reporting.report_service import (REPORTLAB_AVAILABLE,
                                               ReportService)

    if not REPORTLAB_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Report generation requires reportlab. Install with: pip install reportlab",
        )

    try:
        report_service = ReportService()

        # Create output directory
        output_dir = Path("TestOutputs")
        output_dir.mkdir(exist_ok=True)

        # Generate report filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = "".join(
            c if c.isalnum() or c in (" ", "_", "-") else "_" for c in request.tab_title
        )
        safe_title = safe_title[:50]  # Limit length
        filename = f"chat_report_{safe_title}_{timestamp}.pdf"
        output_path = output_dir / filename

        # Convert messages to simple dict format for report
        conversation_history = []
        for msg in request.messages:
            # Extract text content from message
            if isinstance(msg.content, str):
                content_text = msg.content
            else:
                # For content blocks, concatenate text blocks
                text_parts = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        text_parts.append(block.text)
                    elif block.type == "image":
                        text_parts.append("[Image attached]")
                content_text = "\n".join(text_parts)

            conversation_history.append({"role": msg.role, "content": content_text})

        # Generate the report
        success = report_service.generate_investigation_chat_report(
            output_path=output_path,
            tab_title=request.tab_title,
            conversation_history=conversation_history,
            focused_findings=None,  # Could be extended to include findings
            notes=request.notes,
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to generate report")

        return {
            "success": True,
            "filename": filename,
            "path": str(output_path),
            "message": f"Report generated successfully: {filename}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating chat report: {e}")
        raise HTTPException(status_code=500, detail=str(e))
