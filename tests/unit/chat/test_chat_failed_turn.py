"""A turn the agent layer fails is stored as a failed reply, one row per question."""

from __future__ import annotations

import json

import httpx
import pytest

from services.api.routers import claude

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, lines):
        self.lines = lines

    def __call__(self, **_):
        return self

    def stream(self, *_args, **_kw):
        return self

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


async def _relay(monkeypatch, lines, messages):
    saved: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", _Client(lines))
    monkeypatch.setattr(claude, "_internal_headers", lambda: {})
    monkeypatch.setattr(claude, "_persist_chat_turn", lambda **kw: saved.update(kw))
    request = claude.ChatRequest(messages=messages)
    turns = claude._turns_of(request.messages)
    async for _ in claude._relay({"turns": turns}, request, "s-1", "u-1"):
        pass
    return saved


@pytest.mark.asyncio
async def test_an_error_frame_is_stored_as_the_incomplete_reply(monkeypatch):
    error = "the system prompt and tool catalogue leave no room for the current question"
    saved = await _relay(
        monkeypatch,
        [f"data: {json.dumps({'error': error})}"],
        [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
    )
    assert saved["assistant_text"] == error
    assert saved["complete"] is False
    # Two unanswered questions are two rows, not one merged "first\n\nsecond".
    assert saved["user_text"] == "second"


@pytest.mark.asyncio
async def test_an_answered_turn_is_complete(monkeypatch):
    saved = await _relay(
        monkeypatch,
        ['data: {"type": "text", "content": "hello"}'],
        [{"role": "user", "content": "hi"}],
    )
    assert saved["assistant_text"] == "hello"
    assert saved["complete"] is True
