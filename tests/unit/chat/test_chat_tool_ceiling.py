"""A tools array the provider refuses is trimmed by whole servers and retried once (#1226)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from core.integrations.mcp.registry import MCPRegistry
from core.llm.chat_layers import tools_ceiling
from core.llm.tool_schemas import ALL_TOOLS
from services.api.routers import claude

pytestmark = pytest.mark.unit

REJECTION = (
    "400 Invalid 'tools': array too long. Expected an array with maximum length "
    "{max}, but got an array with length {got} instead."
)


def _server(prefix: str, count: int):
    return [
        {"name": f"{prefix}{i}", "description": f"{prefix} {i}"} for i in range(count)
    ]


class _Upstream:
    """The agent layer: refuses a config declaring more than ``ceiling`` tools."""

    def __init__(self, ceiling, reason=REJECTION, always=False):
        self.ceiling, self.reason, self.always, self.posts = ceiling, reason, always, []

    def client(self, **_):
        upstream = self

        class _Response:
            status_code = 200

            def __init__(self, lines):
                self.lines = lines

            async def aiter_lines(self):
                for line in self.lines:
                    yield line

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _Client:
            def stream(self, _method, _url, json, headers):
                tools = [t["id"] for t in yaml.safe_load(json["config"])["tools"]]
                upstream.posts.append(tools)
                windowed = 'data: {"type": "context_windowed", "windowed_messages": 1, "remaining_messages": 1}'
                if upstream.always or len(tools) > upstream.ceiling:
                    error = upstream.reason.format(max=upstream.ceiling, got=len(tools))
                    return _Response([windowed, f"data: {_json({'error': error})}"])
                return _Response(
                    [windowed, 'data: {"type": "text", "content": "answer"}']
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        return _Client()


def _json(event):
    return json.dumps(event)


@pytest.fixture
def registry(monkeypatch):
    reg = MCPRegistry()
    reg.register_server("vigil", {}, _server("vigil_own_", 5))
    reg.register_server("big", {}, _server("t", 40))
    reg.register_server("mid", {}, _server("t", 20))
    reg.register_server("small", {}, _server("t", 3))
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: (None, "m")
    )
    monkeypatch.setattr(
        claude, "provider_for", lambda _: SimpleNamespace(provider_type="openai")
    )
    monkeypatch.setattr(claude, "model_for", lambda _p, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda r: r.get_all_tools())
    monkeypatch.setattr(claude, "_internal_headers", lambda: {})
    monkeypatch.setattr(claude, "_persist_chat_turn", lambda **_: None)
    return reg


async def _turn(monkeypatch, registry, upstream):
    monkeypatch.setattr(httpx, "AsyncClient", upstream.client)
    response = await claude.chat_stream(
        claude.ChatRequest(messages=[{"role": "user", "content": "hunt"}]),
        current_user=SimpleNamespace(username="nestor", user_id="u-1"),
        registry=registry,
    )
    frames = []
    async for chunk in response.body_iterator:
        frames += [
            json.loads(p[6:]) for p in chunk.split("\n\n") if p.startswith("data: ")
        ]
    return frames


BASE = len(ALL_TOOLS) + 5  # built-ins and Vigil's own server are never dropped


@pytest.mark.asyncio
async def test_largest_servers_are_dropped_until_it_fits_and_the_turn_answers(
    monkeypatch, registry
):
    # big (40) alone is not enough to shed 50; mid (20) goes with it, small stays.
    upstream = _Upstream(ceiling=BASE + 13)
    frames = await _turn(monkeypatch, registry, upstream)

    assert len(upstream.posts) == 2
    declared = upstream.posts[1]
    assert not any(t.startswith(("big_", "mid_")) for t in declared)
    assert all(f"small_t{i}" in declared for i in range(3))
    assert all(f"vigil_own_{i}" in declared for i in range(5))
    assert all(
        t["name"] in declared for t in ALL_TOOLS if t["name"] in upstream.posts[0]
    )

    assert not any("error" in f for f in frames)
    texts = [f["content"] for f in frames if f.get("type") == "text"]
    assert "big, mid" in texts[0] and str(BASE + 13) in texts[0]
    assert texts[-1] == "answer"
    # The failed attempt's context_windowed frame is not replayed.
    assert sum(f.get("type") == "context_windowed" for f in frames) == 1


@pytest.mark.asyncio
async def test_a_turn_under_the_ceiling_makes_one_call(monkeypatch, registry):
    upstream = _Upstream(ceiling=1000)
    frames = await _turn(monkeypatch, registry, upstream)
    assert len(upstream.posts) == 1
    assert [f.get("content") for f in frames if f.get("type") == "text"] == ["answer"]


@pytest.mark.asyncio
async def test_no_retry_when_nothing_fits(monkeypatch, registry):
    upstream = _Upstream(ceiling=BASE - 1)
    frames = await _turn(monkeypatch, registry, upstream)
    assert len(upstream.posts) == 1
    assert "maximum length" in frames[-1]["error"]


@pytest.mark.asyncio
async def test_no_retry_for_another_error(monkeypatch, registry):
    upstream = _Upstream(ceiling=BASE, reason="400 model {max} is not found ({got})")
    frames = await _turn(monkeypatch, registry, upstream)
    assert len(upstream.posts) == 1
    assert "not found" in frames[-1]["error"]


@pytest.mark.asyncio
async def test_a_retry_that_fails_too_is_relayed_without_the_note(
    monkeypatch, registry
):
    upstream = _Upstream(ceiling=BASE + 13, always=True)
    frames = await _turn(monkeypatch, registry, upstream)
    assert len(upstream.posts) == 2
    assert not any(f.get("type") == "text" for f in frames)
    assert "maximum length" in frames[-1]["error"]


def test_the_ceiling_is_read_from_the_rejection():
    assert tools_ceiling(REJECTION.format(max=128, got=134)) == 128
    assert tools_ceiling("400 Invalid 'messages': maximum length 10") is None
    per_tool = (
        "400 Invalid 'tools[3].function.name': string too long. "
        "Expected a string with maximum length 64, but got a string with length 70."
    )
    assert tools_ceiling(per_tool) is None
