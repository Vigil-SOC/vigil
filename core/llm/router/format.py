"""Anthropic <-> OpenAI wire-format translation.

The interactive chat path (``OpenAIAgentService``), the daemon's autonomous
tool loop (``daemon/agent_runner`` via ``LLMRouter``), and the workflow engine
all build conversations in **Anthropic** shape — messages whose ``content`` is a
list of typed blocks (``text``, ``thinking``, ``tool_use``, ``tool_result``)
and tools described as ``{"name", "description", "input_schema"}``.

Non-Anthropic providers (Ollama, OpenAI, Groq) are reached through Bifrost's
OpenAI-compatible ``/v1`` surface, which expects a different shape: assistant
``tool_calls`` arrays, standalone ``role: "tool"`` result messages, and tools
described as ``{"type": "function", "function": {...}}``.

Centralizing the translation here keeps the router and the agent loops from
drifting. Both translators are **idempotent / tolerant**: a message whose
``content`` is already a plain string (the ordinary OpenAI shape) passes
through untouched, so callers that never used Anthropic blocks are unaffected.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List


def anthropic_tools_to_openai(
    tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert Anthropic tool schemas to OpenAI function-calling format.

    Anthropic: ``{"name", "description", "input_schema": {json-schema}}``
    OpenAI:    ``{"type": "function", "function": {"name", "description",
               "parameters": {json-schema}}}``

    A tool that is already in OpenAI shape (has a ``function`` key) is passed
    through unchanged so this is safe to call on mixed/pre-converted lists.
    """
    converted: List[Dict[str, Any]] = []
    for tool in tools:
        if "function" in tool and tool.get("type") == "function":
            converted.append(tool)
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema",
                        {"type": "object", "properties": {}},
                    ),
                },
            }
        )
    return converted


def _flatten_tool_result_content(content: Any) -> str:
    """Reduce an Anthropic ``tool_result`` block's content to plain text.

    The content may be a string, or a list of blocks (``{"type": "text",
    "text": ...}``). OpenAI ``tool`` messages carry a single string body.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(json.dumps(block, default=str))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _assistant_block_message(content: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate an Anthropic assistant block list to one OpenAI message."""
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        # 'thinking' blocks have no OpenAI equivalent — drop them.
    msg: Dict[str, Any] = {"role": "assistant"}
    text = "".join(text_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
        # OpenAI requires content present (may be null) alongside tool_calls.
        msg["content"] = text or None
    else:
        # An assistant turn with neither text nor tool calls is invalid; emit
        # an empty string rather than a key-less message.
        msg["content"] = text
    return msg


def _user_block_messages(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate an Anthropic user block list to OpenAI message(s).

    ``tool_result`` blocks become standalone ``role: "tool"`` messages; any
    plain ``text``/``image`` content becomes a regular user message. Tool
    results are emitted first so they immediately follow the assistant
    ``tool_calls`` turn they answer (OpenAI ordering requirement).
    """
    tool_messages: List[Dict[str, Any]] = []
    user_parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            user_parts.append({"type": "text", "text": str(block)})
            continue
        btype = block.get("type")
        if btype == "tool_result":
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _flatten_tool_result_content(block.get("content")),
                }
            )
        elif btype == "text":
            user_parts.append({"type": "text", "text": str(block.get("text", ""))})
        elif btype == "image":
            source = block.get("source", {})
            if isinstance(source, dict) and source.get("type") == "base64":
                media_type = source.get("media_type", "image/jpeg")
                data = source.get("data", "")
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"}
                })
            else:
                user_parts.append(block)
        else:
            user_parts.append({"type": "text", "text": json.dumps(block, default=str)})

    out: List[Dict[str, Any]] = list(tool_messages)
    if user_parts:
        # Collapse a single text part to a plain string (the common case) so
        # the output matches what simple callers already send.
        if len(user_parts) == 1 and user_parts[0].get("type") == "text":
            out.append({"role": "user", "content": user_parts[0]["text"]})
        else:
            out.append({"role": "user", "content": user_parts})
    return out


def anthropic_messages_to_openai(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Translate Anthropic-shape messages to OpenAI chat-completion messages.

    Idempotent for messages whose ``content`` is already a string: those are
    returned unchanged, so this is safe to apply to conversations that never
    used Anthropic content blocks.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            # String content (or anything non-block) — pass through verbatim.
            out.append(msg)
            continue
        if role == "assistant":
            out.append(_assistant_block_message(content))
        elif role == "user":
            out.extend(_user_block_messages(content))
        else:
            # system / tool / unknown roles with block content: flatten to text.
            text = _flatten_tool_result_content(content)
            out.append({"role": role, "content": text})
    return out
