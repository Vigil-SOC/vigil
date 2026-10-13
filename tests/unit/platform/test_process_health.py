"""What GET /api/health says about the agent layer and the SOC daemon.

Each read is the process's own health route: agent-serve's /readyz at AGENT_URL
and the daemon's /health at DAEMON_HEALTH_HOST:DAEMON_HEALTH_PORT. Driven through
httpx.MockTransport, so no socket is opened.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from core.platform import process_health

pytestmark = pytest.mark.unit

AGENT = "http://agent-serve:6989/readyz"
DAEMON = "http://soc-daemon:9091/health"


@pytest.fixture
def stack(monkeypatch):
    """``answers``: URL -> status code, an exception to raise, or "hang"; unlisted
    is refused. ``seen``: the URLs requested."""
    table: dict = {}
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        answer = table.get(url, httpx.ConnectError("refused"))
        if answer == "hang":
            await asyncio.sleep(30)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, text="ok")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        process_health.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(
        process_health,
        "get_settings",
        lambda: SimpleNamespace(
            agent_url="http://agent-serve:6989/",
            daemon_health_host="soc-daemon",
            daemon_health_port=9091,
        ),
    )
    monkeypatch.setattr(process_health, "get_secret", lambda key: "token")
    return SimpleNamespace(answers=table, seen=seen)


def _read() -> dict:
    return asyncio.run(process_health.read_process_health())


def test_both_running(stack):
    stack.answers.update({AGENT: 200, DAEMON: 200})
    assert _read() == {"agent_layer": "running", "daemon": "running"}
    assert sorted(stack.seen) == [AGENT, DAEMON]


def test_refused_is_not_running(stack):
    assert _read() == {"agent_layer": "not_running", "daemon": "not_running"}


def test_answering_but_not_ready_is_unhealthy(stack):
    stack.answers.update({AGENT: 503, DAEMON: 503})
    assert _read() == {"agent_layer": "unhealthy", "daemon": "unhealthy"}


def test_no_token_is_reported_without_calling_the_agent_layer(stack, monkeypatch):
    monkeypatch.setattr(process_health, "get_secret", lambda key: None)
    stack.answers.update({AGENT: 200, DAEMON: 200})
    assert _read() == {"agent_layer": "no_token", "daemon": "running"}
    assert stack.seen == [DAEMON]


def test_a_hung_process_costs_one_timeout_for_both(stack, monkeypatch):
    monkeypatch.setattr(process_health, "READ_TIMEOUT_S", 0.5)
    stack.answers.update({AGENT: "hang", DAEMON: "hang"})
    started = time.monotonic()
    assert _read() == {"agent_layer": "not_running", "daemon": "not_running"}
    assert time.monotonic() - started < 0.9  # one timeout, not two in a row
