"""Claude API endpoints for chat, streaming, and Agent SDK workflows."""

from typing import List, Optional, Dict, Union, Any
from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    File,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
import json
import logging
import base64

from services.claude_service import ClaudeService

router = APIRouter()
logger = logging.getLogger(__name__)


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
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    enable_thinking: bool = False
    thinking_budget: int = 10000
    agent_id: Optional[str] = None
    session_id: Optional[str] = (
        None  # Chat session identifier for reasoning-trace persistence (GH #79)
    )
    streaming: bool = False
    use_agent_sdk: bool = False  # Enable Agent SDK for agentic workflows


class AgentTaskRequest(BaseModel):
    """Request for running an agent task."""

    task: str
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    max_turns: int = 10
    model: str = "claude-sonnet-4-20250514"
    session_id: Optional[str] = None
    agent_id: Optional[str] = None


@router.post("/chat")
async def chat(request: ChatRequest):
    """
    Send a chat message to Claude and get a response.

    Supports both standard chat and Agent SDK mode for agentic workflows.

    Args:
        request: Chat request with messages and parameters

    Returns:
        Claude's response
    """
    from services.soc_agents import AgentManager
    import uuid
    import time

    # Generate unique request ID for tracking
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()

    logger.info(
        f"📨 Chat request received - RequestID: {request_id}, Model: {request.model}, Thinking: {request.enable_thinking}, Budget: {request.thinking_budget}, Agent: {request.agent_id}"
    )

    # Log full request payload (truncate long messages for readability)
    logger.info(f"🔍 [RequestID: {request_id}] Full request payload:")
    logger.info(f"  - Messages count: {len(request.messages)}")
    for i, msg in enumerate(request.messages):
        content_preview = (
            str(msg.content)[:200] + "..."
            if len(str(msg.content)) > 200
            else str(msg.content)
        )
        logger.info(f"  - Message {i} [{msg.role}]: {content_preview}")
    logger.info(
        f"  - System prompt: {request.system_prompt[:100] if request.system_prompt else 'None'}..."
    )
    logger.info(f"  - Max tokens: {request.max_tokens}")
    logger.info(f"  - Streaming: {request.streaming}")
    logger.info(f"  - Agent SDK: {request.use_agent_sdk}")

    # If agent_id is provided, get the agent's system prompt and settings
    system_prompt = request.system_prompt
    enable_thinking = request.enable_thinking
    max_tokens = request.max_tokens
    thinking_budget = request.thinking_budget
    allowed_tools = None

    if request.agent_id:
        agent_manager = AgentManager()
        agent = agent_manager.agents.get(request.agent_id)
        if agent:
            system_prompt = agent.system_prompt
            allowed_tools = agent.recommended_tools
            # Per GH #79: per-interaction thinking override — request wins verbatim,
            # never downgraded or upgraded from the agent default. Callers control
            # thinking explicitly per call.
            if request.max_tokens == 4096:  # schema default — use agent's value
                max_tokens = agent.max_tokens
            logger.info(
                f"🤖 Using agent: {agent.name} (thinking: {enable_thinking}, max_tokens: {max_tokens})"
            )

            # Ensure thinking_budget is less than max_tokens
            if enable_thinking and thinking_budget and thinking_budget >= max_tokens:
                thinking_budget = int(max_tokens * 0.6)
                logger.warning(
                    f"⚠️ Adjusted thinking_budget from {request.thinking_budget} to {thinking_budget}"
                )

    claude_service = ClaudeService(
        use_backend_tools=True,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
        use_agent_sdk=request.use_agent_sdk,
    )

    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

    try:
        # Convert messages to format expected by Claude
        messages = []
        for msg in request.messages:
            if isinstance(msg.content, str):
                messages.append({"role": msg.role, "content": msg.content})
            else:
                content_blocks = []
                for block in msg.content:
                    if block.type == "text":
                        content_blocks.append({"type": "text", "text": block.text})
                    elif block.type == "image" and block.source:
                        content_blocks.append({"type": "image", "source": block.source})
                    elif block.type == "thinking":
                        continue
                if content_blocks:
                    messages.append({"role": msg.role, "content": content_blocks})

        if len(messages) == 0:
            raise HTTPException(status_code=400, detail="No messages provided")

        # Validate message sequence - ensure no consecutive same-role messages
        validated_messages = []
        for msg in messages:
            if validated_messages and validated_messages[-1]["role"] == msg["role"]:
                logger.warning(
                    f"⚠️ Consecutive {msg['role']} messages detected in chat, merging"
                )
                prev = validated_messages[-1]
                if isinstance(prev["content"], str) and isinstance(msg["content"], str):
                    prev["content"] = prev["content"] + "\n\n" + msg["content"]
                elif isinstance(prev["content"], list) and isinstance(
                    msg["content"], list
                ):
                    prev["content"] = prev["content"] + msg["content"]
                else:
                    prev_blocks = (
                        [{"type": "text", "text": prev["content"]}]
                        if isinstance(prev["content"], str)
                        else prev["content"]
                    )
                    new_blocks = (
                        [{"type": "text", "text": msg["content"]}]
                        if isinstance(msg["content"], str)
                        else msg["content"]
                    )
                    prev["content"] = prev_blocks + new_blocks
            else:
                validated_messages.append(msg)
        messages = validated_messages

        if len(messages) == 0:
            raise HTTPException(
                status_code=400, detail="No valid messages after filtering"
            )

        current_message = messages[-1]["content"]
        context = messages[:-1] if len(messages) > 1 else None

        # Use Agent SDK for agentic workflows
        if request.use_agent_sdk and claude_service.use_agent_sdk:
            # Extract text from current message for agent query
            if isinstance(current_message, list):
                prompt = " ".join(
                    [
                        b.get("text", "")
                        for b in current_message
                        if b.get("type") == "text"
                    ]
                )
            else:
                prompt = current_message

            result = await claude_service.run_agent_task(
                task=prompt,
                agent_config={
                    "system_prompt": system_prompt,
                    "allowed_tools": allowed_tools,
                    "model": request.model,
                },
            )

            return {
                "response": result.get("final_result", ""),
                "model": request.model,
                "agent_id": request.agent_id,
                "agent_sdk": True,
                "tool_calls": result.get("tool_calls", []),
            }

        # Standard chat mode -- route through LLM queue for global rate limiting
        logger.info(
            f"💬 Starting chat with {len(messages)} messages, thinking={enable_thinking}, budget={thinking_budget}"
        )
        from services.llm_gateway import get_llm_gateway

        gateway = await get_llm_gateway()
        response = await gateway.submit_chat(
            messages=messages,
            model=request.model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            session_id=request.session_id,
            agent_id=request.agent_id,
        )
        # Unwrap gateway response envelope
        if isinstance(response, dict) and "content" in response:
            response = response["content"]

        # Log response type and structure
        elapsed_time = time.time() - start_time
        response_type = type(response).__name__

        logger.info(
            f"✅ [RequestID: {request_id}] Chat complete in {elapsed_time:.2f}s - Response type: {response_type}"
        )

        # Log detailed response structure
        if isinstance(response, list):
            logger.info(
                f"📦 [RequestID: {request_id}] Response: list with {len(response)} blocks"
            )
            for i, block in enumerate(response):
                if isinstance(block, dict):
                    block_type = block.get("type", "unknown")
                    text_len = len(block.get("text", ""))
                    text_preview = (
                        block.get("text", "")[:200] + "..."
                        if text_len > 200
                        else block.get("text", "")
                    )
                    logger.info(f"  📄 Block {i} [{block_type}]: {text_len} chars")
                    logger.info(f"     Preview: {text_preview}")
        elif isinstance(response, str):
            logger.info(
                f"📦 [RequestID: {request_id}] Response: string with {len(response)} chars"
            )
            logger.info(
                f"     Preview: {response[:200]}..."
                if len(response) > 200
                else f"     Content: {response}"
            )
        else:
            logger.info(f"📦 [RequestID: {request_id}] Response type: {response_type}")

        return {
            "response": response,
            "model": request.model,
            "agent_id": request.agent_id,
            "agent_sdk": False,
        }

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(
            f"❌ [RequestID: {request_id}] Chat error after {elapsed_time:.2f}s: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    Send a chat message to Claude and stream the response.

    Args:
        request: Chat request with messages and parameters

    Returns:
        Streaming response
    """
    import uuid
    import time

    # Generate unique request ID for tracking
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()

    logger.info(
        f"🌊 Stream request received - RequestID: {request_id}, Model: {request.model}, Thinking: {request.enable_thinking}, Budget: {request.thinking_budget}, Agent: {request.agent_id}"
    )

    # Log full request payload
    logger.info(f"🔍 [RequestID: {request_id}] Stream request payload:")
    logger.info(f"  - Messages count: {len(request.messages)}")
    for i, msg in enumerate(request.messages):
        content_preview = (
            str(msg.content)[:200] + "..."
            if len(str(msg.content)) > 200
            else str(msg.content)
        )
        logger.info(f"  - Message {i} [{msg.role}]: {content_preview}")
    logger.info(
        f"  - System prompt: {request.system_prompt[:100] if request.system_prompt else 'None'}..."
    )
    logger.info(f"  - Max tokens: {request.max_tokens}")

    # If agent_id is provided, get the agent's system prompt and settings
    system_prompt = request.system_prompt
    # None means UI didn't set a value — resolve to agent default or global fallback below
    enable_thinking = request.enable_thinking
    max_tokens = request.max_tokens
    thinking_budget = request.thinking_budget

    if request.agent_id:
        from services.soc_agents import AgentManager

        agent_manager = AgentManager()
        agent = agent_manager.agents.get(request.agent_id)
        if agent:
            system_prompt = agent.system_prompt
            # Per GH #79: request wins verbatim (no downgrade/upgrade from agent default)
            if max_tokens is None:
                max_tokens = agent.max_tokens
            logger.info(
                f"🤖 Stream using agent: {agent.name} (thinking: {enable_thinking}, max_tokens: {max_tokens})"
            )

            # Ensure thinking_budget is less than max_tokens
            if enable_thinking and thinking_budget and thinking_budget >= max_tokens:
                thinking_budget = int(max_tokens * 0.6)
                logger.warning(
                    f"⚠️ Adjusted stream thinking_budget from {request.thinking_budget} to {thinking_budget}"
                )

    # Final fallback if no agent set these
    if enable_thinking is None:
        enable_thinking = False
    if max_tokens is None:
        max_tokens = 4096

    claude_service = ClaudeService(
        use_backend_tools=True,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
    )

    # Check if API key is configured (works for both implementations)
    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

    async def generate():
        try:
            # Convert messages to format expected by Claude
            messages = []
            for msg in request.messages:
                if isinstance(msg.content, str):
                    messages.append({"role": msg.role, "content": msg.content})
                else:
                    # Handle content blocks (text + images, skip thinking blocks)
                    content_blocks = []
                    for block in msg.content:
                        if block.type == "text":
                            content_blocks.append({"type": "text", "text": block.text})
                        elif block.type == "image" and block.source:
                            content_blocks.append(
                                {"type": "image", "source": block.source}
                            )
                        # Skip thinking blocks - they should not be included in requests
                        elif block.type == "thinking":
                            continue

                    # Only add message if it has content after filtering
                    if content_blocks:
                        messages.append({"role": msg.role, "content": content_blocks})

            # Split messages into context and current message
            if len(messages) == 0:
                logger.error("❌ No messages provided in stream request")
                yield f"data: {json.dumps({'error': 'No messages provided'})}\n\n"
                return

            # Validate message sequence - ensure no consecutive same-role messages
            validated_messages = []
            for msg in messages:
                if validated_messages and validated_messages[-1]["role"] == msg["role"]:
                    # Merge consecutive same-role messages or skip empty ones
                    logger.warning(
                        f"⚠️ Consecutive {msg['role']} messages detected, merging"
                    )
                    prev = validated_messages[-1]
                    if isinstance(prev["content"], str) and isinstance(
                        msg["content"], str
                    ):
                        prev["content"] = prev["content"] + "\n\n" + msg["content"]
                    elif isinstance(prev["content"], list) and isinstance(
                        msg["content"], list
                    ):
                        prev["content"] = prev["content"] + msg["content"]
                    else:
                        # Mixed types - convert to list
                        prev_blocks = (
                            [{"type": "text", "text": prev["content"]}]
                            if isinstance(prev["content"], str)
                            else prev["content"]
                        )
                        new_blocks = (
                            [{"type": "text", "text": msg["content"]}]
                            if isinstance(msg["content"], str)
                            else msg["content"]
                        )
                        prev["content"] = prev_blocks + new_blocks
                else:
                    validated_messages.append(msg)
            messages = validated_messages

            if len(messages) == 0:
                logger.error("❌ No valid messages after validation")
                yield f"data: {json.dumps({'error': 'No valid messages after filtering'})}\n\n"
                return

            # Get the last message as the current message
            current_message = messages[-1]["content"]

            # Use all previous messages as context (if any)
            context = messages[:-1] if len(messages) > 1 else None

            logger.info(
                f"🚀 [RequestID: {request_id}] Starting stream with {len(messages)} messages, context={len(context) if context else 0}"
            )

            chunk_count = 0
            thinking_chunks = 0
            text_chunks = 0
            total_text_length = 0
            total_thinking_length = 0

            async for chunk in claude_service.chat_stream(
                message=current_message,
                context=context,
                system_prompt=system_prompt,
                model=request.model,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                session_id=request.session_id,
                agent_id=request.agent_id,
            ):
                chunk_count += 1
                # Handle both dict (new format with thinking) and string (backward compat)
                if isinstance(chunk, dict):
                    chunk_type = chunk.get("type", "unknown")
                    chunk_content = chunk.get("content", "")

                    if chunk_type == "thinking":
                        thinking_chunks += 1
                        total_thinking_length += len(chunk_content)
                        if thinking_chunks <= 3:  # Log first few
                            logger.debug(
                                f"💭 [RequestID: {request_id}] Thinking chunk {thinking_chunks}: {chunk_content[:50]}..."
                            )
                    elif chunk_type == "text":
                        text_chunks += 1
                        total_text_length += len(chunk_content)
                        if text_chunks <= 3:  # Log first few
                            logger.debug(
                                f"📝 [RequestID: {request_id}] Text chunk {text_chunks}: {chunk_content[:50]}..."
                            )
                    elif chunk_type in ["thinking_start", "thinking_end"]:
                        logger.info(
                            f"🔄 [RequestID: {request_id}] Stream event: {chunk_type}"
                        )
                    elif chunk_type == "context_summarized":
                        logger.info(
                            f"📝 [RequestID: {request_id}] Context auto-summarized: {chunk.get('summarized_messages', 0)} messages condensed"
                        )

                    yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    # Old format - plain text string
                    text_chunks += 1
                    total_text_length += len(chunk)
                    if text_chunks <= 3:
                        logger.debug(
                            f"📝 [RequestID: {request_id}] Text chunk (legacy) {text_chunks}: {chunk[:50]}..."
                        )
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"

            elapsed_time = time.time() - start_time
            logger.info(
                f"✅ [RequestID: {request_id}] Stream complete in {elapsed_time:.2f}s"
            )
            logger.info(
                f"   📊 Stats: Total chunks: {chunk_count}, Thinking: {thinking_chunks} ({total_thinking_length} chars), Text: {text_chunks} ({total_text_length} chars)"
            )

        except Exception as e:
            elapsed_time = time.time() - start_time
            logger.error(
                f"❌ [RequestID: {request_id}] Stream error after {elapsed_time:.2f}s: {e}",
                exc_info=True,
            )
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for bidirectional chat with Claude.

    This allows for streaming responses and real-time interaction.
    """
    await websocket.accept()

    claude_service = ClaudeService(use_backend_tools=True)

    # Check if API key is configured (works for both implementations)
    if not claude_service.has_api_key():
        await websocket.send_json({"error": "Claude API not configured"})
        await websocket.close()
        return

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()

            messages = data.get("messages", [])
            system_prompt = data.get("system_prompt")
            model = data.get("model", "claude-sonnet-4-20250514")
            max_tokens = data.get("max_tokens", 4096)
            enable_thinking = data.get("enable_thinking", False)
            thinking_budget = data.get("thinking_budget", 10000)

            # Update thinking settings if needed
            if enable_thinking != claude_service.enable_thinking:
                claude_service.enable_thinking = enable_thinking
                claude_service.thinking_budget = thinking_budget

            # Stream response back to client
            try:
                # Split messages into context and current message
                if len(messages) == 0:
                    await websocket.send_json({"error": "No messages provided"})
                    continue

                # Get the last message as the current message
                current_message = messages[-1]["content"]

                # Use all previous messages as context (if any)
                context = messages[:-1] if len(messages) > 1 else None

                async for chunk in claude_service.chat_stream(
                    message=current_message,
                    context=context,
                    system_prompt=system_prompt,
                    model=model,
                    max_tokens=max_tokens,
                ):
                    # Handle both dict (new format with thinking) and string (backward compat)
                    if isinstance(chunk, dict):
                        await websocket.send_json(chunk)
                    else:
                        # Old format - plain text string
                        await websocket.send_json({"type": "text", "content": chunk})

            except Exception as e:
                logger.error(f"Error in chat stream: {e}")
                await websocket.send_json({"error": str(e)})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await websocket.close()


@router.get("/models")
async def get_models():
    """
    Get list of available Claude models.

    Returns:
        List of model names
    """
    return {
        "models": [
            {
                "id": "claude-sonnet-4-20250514",
                "name": "Claude 4.5 Sonnet",
                "description": "Most intelligent model, best for complex tasks",
            },
            {
                "id": "claude-3-5-sonnet-20241022",
                "name": "Claude 3.5 Sonnet",
                "description": "Previous generation, good balance of speed and intelligence",
            },
            {
                "id": "claude-3-5-haiku-20241022",
                "name": "Claude 3.5 Haiku",
                "description": "Fastest model, good for simple tasks",
            },
        ]
    }


class SummarizeRequest(BaseModel):
    """Request to summarize a conversation."""

    messages: List[ChatMessage]
    model: str = "claude-sonnet-4-20250514"


@router.post("/summarize")
async def summarize_conversation(request: SummarizeRequest):
    """
    Summarize a conversation into a condensed context message.

    Used when conversations approach the context window limit.
    Returns a single summary message that preserves key context.
    """
    import asyncio

    claude_service = ClaudeService(use_backend_tools=False, enable_thinking=False)

    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

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
        from services.llm_gateway import get_llm_gateway

        gateway = await get_llm_gateway()
        response = await gateway.submit_chat(
            messages=[{"role": "user", "content": summary_prompt}],
            model=request.model,
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


@router.get("/sdk-status")
async def get_sdk_status():
    """Check availability of Claude Agent SDK."""
    return {
        "agent_sdk_available": ClaudeService.is_agent_sdk_available(),
        "anthropic_available": ClaudeService(use_backend_tools=True).has_api_key(),
    }


@router.post("/agent/task")
async def run_agent_task(request: AgentTaskRequest):
    """
    Run an agentic task using Claude Agent SDK.

    This endpoint executes a task with the Agent SDK, allowing Claude to
    use tools autonomously to complete the task.

    Args:
        request: Agent task request with task description and configuration

    Returns:
        Task result with tool calls and final output
    """
    from services.soc_agents import AgentManager

    system_prompt = request.system_prompt
    allowed_tools = request.allowed_tools
    max_turns = request.max_turns

    # Apply agent configuration if specified
    if request.agent_id:
        agent_manager = AgentManager()
        agent = agent_manager.agents.get(request.agent_id)
        if agent:
            system_prompt = system_prompt or agent.system_prompt
            if not allowed_tools:
                allowed_tools = agent.recommended_tools
            logger.info(f"Using agent: {agent.name}")

    claude_service = ClaudeService(use_backend_tools=True, use_agent_sdk=True)

    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

    try:
        result = await claude_service.run_agent_task(
            task=request.task,
            agent_config={
                "system_prompt": system_prompt,
                "allowed_tools": allowed_tools,
                "max_turns": max_turns,
                "model": request.model,
            },
            session_id=request.session_id,
        )

        return {
            "success": result.get("success", False),
            "task": request.task,
            "result": result.get("final_result", ""),
            "tool_calls": result.get("tool_calls", []),
            "error": result.get("error"),
            "agent_id": request.agent_id,
            "session_id": request.session_id,
        }

    except Exception as e:
        logger.error(f"Agent task error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/stream")
async def stream_agent_task(request: AgentTaskRequest):
    """
    Stream an agentic task using Claude Agent SDK.

    This endpoint streams task execution events in real-time,
    including tool calls and intermediate results.

    Args:
        request: Agent task request with task description and configuration

    Returns:
        Streaming response with task events
    """
    from services.soc_agents import AgentManager

    system_prompt = request.system_prompt
    allowed_tools = request.allowed_tools

    if request.agent_id:
        agent_manager = AgentManager()
        agent = agent_manager.agents.get(request.agent_id)
        if agent:
            system_prompt = system_prompt or agent.system_prompt
            if not allowed_tools:
                allowed_tools = agent.recommended_tools

    claude_service = ClaudeService(use_backend_tools=True, use_agent_sdk=True)

    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

    async def generate():
        try:
            async for event in claude_service.agent_query(
                prompt=request.task,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                max_turns=request.max_turns,
                session_id=request.session_id,
                model=request.model,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.error(f"Agent stream error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    """
    WebSocket endpoint for interactive agent sessions.

    Supports bidirectional communication for multi-turn agent workflows
    with real-time streaming of tool calls and results.
    """
    await websocket.accept()

    claude_service = ClaudeService(use_backend_tools=True, use_agent_sdk=True)

    if not claude_service.has_api_key():
        await websocket.send_json(
            {"type": "error", "content": "Claude API not configured"}
        )
        await websocket.close()
        return

    session_id = None

    try:
        while True:
            data = await websocket.receive_json()

            task = data.get("task", "")
            system_prompt = data.get("system_prompt")
            allowed_tools = data.get("allowed_tools")
            max_turns = data.get("max_turns", 10)
            model = data.get("model", "claude-sonnet-4-20250514")
            agent_id = data.get("agent_id")

            # Handle session management
            if data.get("action") == "new_session":
                session_id = f"ws-{id(websocket)}-{hash(task)}"
                claude_service.create_session(session_id)
                await websocket.send_json({"type": "session", "session_id": session_id})
                continue
            elif data.get("action") == "clear_session":
                if session_id:
                    claude_service.clear_session(session_id)
                await websocket.send_json({"type": "session_cleared"})
                continue

            # Apply agent config
            if agent_id:
                from services.soc_agents import AgentManager

                agent_manager = AgentManager()
                agent = agent_manager.agents.get(agent_id)
                if agent:
                    system_prompt = system_prompt or agent.system_prompt
                    if not allowed_tools:
                        allowed_tools = agent.recommended_tools

            try:
                async for event in claude_service.agent_query(
                    prompt=task,
                    system_prompt=system_prompt,
                    allowed_tools=allowed_tools,
                    max_turns=max_turns,
                    session_id=session_id,
                    model=model,
                ):
                    await websocket.send_json(event)

                await websocket.send_json({"type": "complete"})

            except Exception as e:
                logger.error(f"Agent execution error: {e}")
                await websocket.send_json({"type": "error", "content": str(e)})

    except WebSocketDisconnect:
        logger.info("Agent WebSocket disconnected")
        if session_id:
            claude_service.clear_session(session_id)
    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")
        await websocket.close()


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
    from services.database_data_service import DatabaseDataService

    data_service = DatabaseDataService()
    finding = data_service.get_finding(finding_id)

    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    claude_service = ClaudeService(use_backend_tools=True)

    # Check if API key is configured (works for both implementations)
    if not claude_service.has_api_key():
        raise HTTPException(status_code=503, detail="Claude API not configured")

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
        from services.llm_gateway import get_llm_gateway

        gateway = await get_llm_gateway()
        response = await gateway.submit_chat(
            messages=[{"role": "user", "content": prompt}],
            model="claude-sonnet-4-20250514",
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
    from services.report_service import ReportService, REPORTLAB_AVAILABLE
    from pathlib import Path
    from datetime import datetime

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
