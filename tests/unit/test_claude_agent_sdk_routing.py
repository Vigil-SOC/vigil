"""Unit tests for the #413 4c-2 migration of the Agent SDK endpoints in
``backend/api/claude.py`` onto ``LLMRouter``.

The ``/agent/task`` and ``/agent/stream`` handlers used to construct
``ClaudeService(use_backend_tools=True, use_agent_sdk=True)`` directly and gate
on ``claude_service.has_api_key()``. After 4c-2 they dispatch through
``LLMRouter().run_agent_task`` / ``run_agent_stream`` and gate on the
``anthropic_api_key_available()`` helper — no ``ClaudeService`` construction in
the endpoint layer. These tests pin that behaviour by driving the handler
coroutines directly with the router + gate patched, so no DB/auth/SDK is
required.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
os.environ.setdefault("DEV_MODE", "true")
for _p in (str(REPO), str(REPO / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import HTTPException  # noqa: E402

import services.llm_router as llm_router  # noqa: E402

pytestmark = pytest.mark.unit


def _load_claude_module():
    spec = importlib.util.spec_from_file_location(
        "claude_api_sdk_under_test", str(REPO / "backend" / "api" / "claude.py")
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"backend.api.claude not importable here: {exc}",
            allow_module_level=True,
        )
    return mod


def _load_agents_module():
    spec = importlib.util.spec_from_file_location(
        "agents_api_sdk_under_test", str(REPO / "backend" / "api" / "agents.py")
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"backend.api.agents not importable here: {exc}",
            allow_module_level=True,
        )
    return mod


claude = _load_claude_module()
agents = _load_agents_module()


class _FakeRouter:
    """Records the last agent_config and returns canned SDK results."""

    last_task_config = None
    last_stream_kwargs = None

    def __init__(self, *args, **kwargs):
        pass

    async def run_agent_task(self, task, agent_config=None, session_id=None):
        type(self).last_task_config = dict(agent_config or {})
        return {
            "success": True,
            "task": task,
            "final_result": "done",
            "tool_calls": [{"tool": "get_finding"}],
            "error": None,
        }

    async def run_agent_stream(self, prompt, **kwargs):
        type(self).last_stream_kwargs = dict(kwargs)
        for evt in (
            {"type": "tool_use", "tool": "get_finding"},
            {"type": "result", "content": "final"},
        ):
            yield evt

    async def dispatch_stream(self, **kwargs):
        type(self).last_stream_kwargs = dict(kwargs)
        # Mixed event vocabulary post-3b: text/thinking carry content;
        # tool_processing has no "content" key (old append-all was a no-op).
        for evt in (
            {"type": "text", "content": "Hello "},
            {"type": "thinking", "content": "pondering"},
            {"type": "tool_processing", "tool_name": "x", "tool_id": "1"},
            {"type": "text", "content": "world"},
        ):
            yield evt


class _StubUser:
    user_id = "u1"


def _patch_router(monkeypatch, *, key_available: bool):
    _FakeRouter.last_task_config = None
    _FakeRouter.last_stream_kwargs = None
    monkeypatch.setattr(llm_router, "LLMRouter", _FakeRouter)
    monkeypatch.setattr(
        llm_router, "anthropic_api_key_available", lambda: key_available
    )
    # The endpoints resolve the model via the registry; pin it so no DB is hit.
    monkeypatch.setattr(claude, "_resolve_model_for_request", lambda m, a: "m")


# --- /agent/task ------------------------------------------------------------


def test_agent_task_no_key_raises_503(monkeypatch):
    _patch_router(monkeypatch, key_available=False)
    req = claude.AgentTaskRequest(task="investigate")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(claude.run_agent_task(req))
    assert exc.value.status_code == 503


def test_agent_task_dispatches_via_router_with_backend_tools(monkeypatch):
    _patch_router(monkeypatch, key_available=True)
    req = claude.AgentTaskRequest(task="investigate", model="m")
    result = asyncio.run(claude.run_agent_task(req))
    # Mapped from the router's contract dict, not a ClaudeService.
    assert result["success"] is True
    assert result["result"] == "done"
    assert result["tool_calls"] == [{"tool": "get_finding"}]
    # 4c-2 mirrors the old ClaudeService(use_backend_tools=True) construction.
    assert _FakeRouter.last_task_config["use_backend_tools"] is True


# --- /agent/stream ----------------------------------------------------------


def test_agent_stream_no_key_raises_503(monkeypatch):
    _patch_router(monkeypatch, key_available=False)
    req = claude.AgentTaskRequest(task="investigate")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(claude.stream_agent_task(req))
    assert exc.value.status_code == 503


def test_agent_stream_frames_router_events(monkeypatch):
    _patch_router(monkeypatch, key_available=True)
    req = claude.AgentTaskRequest(task="investigate", model="m")
    response = asyncio.run(claude.stream_agent_task(req))

    async def _drain():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(_drain())
    payloads = [json.loads(c.removeprefix("data: ").strip()) for c in chunks]
    assert {"type": "tool_use", "tool": "get_finding"} in payloads
    assert {"type": "result", "content": "final"} in payloads
    # use_backend_tools threaded through agent_config to the SDK engine.
    assert _FakeRouter.last_stream_kwargs["agent_config"] == {"use_backend_tools": True}


# --- /chat use_agent_sdk branch ---------------------------------------------


def test_chat_agent_sdk_branch_dispatches_via_router(monkeypatch):
    _patch_router(monkeypatch, key_available=True)
    monkeypatch.setattr(llm_router, "agent_sdk_available", lambda: True)
    # No positively-identified non-Anthropic provider -> use_router False, so
    # the SDK branch (not the router dispatch branch) is taken.
    monkeypatch.setattr(claude, "_select_active_provider", lambda pid: None)
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda m, a: (None, "m")
    )
    req = claude.ChatRequest(
        messages=[claude.ChatMessage(role="user", content="investigate this")],
        use_agent_sdk=True,
        model="m",
    )
    result = asyncio.run(claude.chat(req))
    assert result["agent_sdk"] is True
    assert result["response"] == "done"
    assert result["tool_calls"] == [{"tool": "get_finding"}]
    assert _FakeRouter.last_task_config["use_backend_tools"] is True


def test_chat_no_key_raises_503_before_sdk_dispatch(monkeypatch):
    _patch_router(monkeypatch, key_available=False)
    monkeypatch.setattr(llm_router, "agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude, "_select_active_provider", lambda pid: None)
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda m, a: (None, "m")
    )
    req = claude.ChatRequest(
        messages=[claude.ChatMessage(role="user", content="hi")],
        use_agent_sdk=True,
        model="m",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(claude.chat(req))
    assert exc.value.status_code == 503


# --- agents.py /agents/run --------------------------------------------------


class _StubAgent:
    id = "investigator"
    name = "Investigator"
    icon = "🕵️"
    color = "#abc"
    system_prompt = "you investigate"
    recommended_tools = ["get_finding"]
    enable_thinking = True


def test_agents_run_dispatches_via_router_with_flags(monkeypatch):
    _patch_router(monkeypatch, key_available=True)
    monkeypatch.setattr(agents, "_resolve_agent", lambda aid: _StubAgent())
    req = agents.AgentRunRequest(task="do the thing", agent_id="investigator")
    result = asyncio.run(agents.run_agent(req))
    assert result["success"] is True
    assert result["result"] == "done"
    assert result["agent_sdk_used"] is True
    cfg = _FakeRouter.last_task_config
    # agents.py mirrors ClaudeService(use_backend_tools=True, use_mcp_tools=True,
    # enable_thinking=agent.enable_thinking) through agent_config.
    assert cfg["use_backend_tools"] is True
    assert cfg["use_mcp_tools"] is True
    assert cfg["enable_thinking"] is True


def test_agents_run_no_key_raises_503(monkeypatch):
    _patch_router(monkeypatch, key_available=False)
    monkeypatch.setattr(agents, "_resolve_agent", lambda aid: _StubAgent())
    req = agents.AgentRunRequest(task="do the thing", agent_id="investigator")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(agents.run_agent(req))
    assert exc.value.status_code == 503


# --- /chat/stream (4c-3) ----------------------------------------------------


def _patch_stream(monkeypatch, *, key_available: bool):
    _patch_router(monkeypatch, key_available=key_available)
    # No positively-identified non-Anthropic provider -> use_router False so the
    # H1 503 gate applies; dispatch_stream still handles the Anthropic path.
    monkeypatch.setattr(claude, "_select_active_provider", lambda pid: None)
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda m, a: (None, "m")
    )


def test_chat_stream_no_key_raises_503(monkeypatch):
    _patch_stream(monkeypatch, key_available=False)
    req = claude.ChatRequest(
        messages=[claude.ChatMessage(role="user", content="hi")], model="m"
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(claude.chat_stream(req, _StubUser()))
    assert exc.value.status_code == 503


def test_chat_stream_dispatches_frames_and_accumulates_history(monkeypatch):
    _patch_stream(monkeypatch, key_available=True)
    captured = {}
    monkeypatch.setattr(claude, "_persist_chat_turn", lambda **kw: captured.update(kw))

    req = claude.ChatRequest(
        messages=[claude.ChatMessage(role="user", content="analyze")],
        model="m",
        session_id="s1",
    )
    response = asyncio.run(claude.chat_stream(req, _StubUser()))

    async def _drain():
        return [c async for c in response.body_iterator]

    frames = asyncio.run(_drain())
    payloads = [json.loads(f.removeprefix("data: ").strip()) for f in frames]
    # Every chunk is framed to the client verbatim, in order.
    assert [p.get("type") for p in payloads] == [
        "text",
        "thinking",
        "tool_processing",
        "text",
    ]
    # Type-based history accumulation: text concatenated to assistant, thinking
    # separated, tool_processing (no "content") contributes nothing.
    assert captured["assistant_text"] == "Hello world"
    assert captured["assistant_thinking"] == "pondering"
    assert captured["complete"] is True
    # recommended_tools + the full validated messages are threaded to the router.
    assert "recommended_tools" in _FakeRouter.last_stream_kwargs
    assert _FakeRouter.last_stream_kwargs["provider"] is None
