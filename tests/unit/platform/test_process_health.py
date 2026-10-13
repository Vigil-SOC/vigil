"""What GET /api/processes says about the agent layer and the SOC daemon.

Each read is the process's own health route: agent-serve's /readyz at AGENT_URL
and the daemon's /health at DAEMON_HEALTH_HOST:DAEMON_HEALTH_PORT. The client is
injected with an httpx.MockTransport, so no socket is opened.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from core.platform import process_health
from core.platform.process_health import ProcessHealth

pytestmark = pytest.mark.unit

AGENT = "http://agent-serve:6989/readyz"
DAEMON = "http://soc-daemon:9091/health"


@pytest.fixture
def stack(monkeypatch):
    """``answers``: URL -> status code, an exception to raise, or "hang"; unlisted
    is refused. ``seen``: the URLs requested. ``health``: a ProcessHealth on it."""
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
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SimpleNamespace(answers=table, seen=seen, health=ProcessHealth(client))


def _read(stack) -> dict:
    return asyncio.run(stack.health.read())


def test_both_running(stack):
    stack.answers.update({AGENT: 200, DAEMON: 200})
    assert _read(stack) == {"agent_layer": "running", "daemon": "running"}
    assert sorted(stack.seen) == [AGENT, DAEMON]


def test_refused_or_unresolvable_is_not_running(stack):
    stack.answers[DAEMON] = httpx.ConnectError("nodename nor servname provided")
    assert _read(stack) == {"agent_layer": "not_running", "daemon": "not_running"}


def test_answering_but_not_ready_is_unhealthy(stack):
    stack.answers.update({AGENT: 503, DAEMON: 503})
    assert _read(stack) == {"agent_layer": "unhealthy", "daemon": "unhealthy"}


def test_no_token_is_reported_without_calling_the_agent_layer(stack, monkeypatch):
    monkeypatch.setattr(process_health, "get_secret", lambda key: None)
    stack.answers.update({AGENT: 200, DAEMON: 200})
    assert _read(stack) == {"agent_layer": "no_token", "daemon": "running"}
    assert stack.seen == [DAEMON]


def test_a_hung_process_is_unhealthy_and_both_cost_one_timeout(stack, monkeypatch):
    monkeypatch.setattr(process_health, "READ_TIMEOUT_S", 0.5)
    stack.answers.update({AGENT: "hang", DAEMON: "hang"})
    started = time.monotonic()
    assert _read(stack) == {"agent_layer": "unhealthy", "daemon": "unhealthy"}
    assert time.monotonic() - started < 0.9  # one timeout, not two in a row


def test_an_error_reads_as_unknown_and_never_raises(stack, monkeypatch):
    def broken():
        raise ValueError("bad DAEMON_HEALTH_PORT")

    monkeypatch.setattr(process_health, "get_settings", broken)
    assert _read(stack) == {"agent_layer": "unknown", "daemon": "unknown"}


def test_a_secrets_error_hides_only_the_agent_layer(stack, monkeypatch):
    def broken(key):
        raise OSError("secrets.enc unreadable")

    monkeypatch.setattr(process_health, "get_secret", broken)
    stack.answers.update({AGENT: 200, DAEMON: 503})
    assert _read(stack) == {"agent_layer": "unknown", "daemon": "unhealthy"}
    assert stack.seen == [DAEMON]


def test_callers_share_one_read(stack):
    stack.answers.update({AGENT: 200, DAEMON: 200})

    async def many():
        return await asyncio.gather(*(stack.health.read() for _ in range(20)))

    answers = asyncio.run(many())
    assert all(a == {"agent_layer": "running", "daemon": "running"} for a in answers)
    assert sorted(stack.seen) == [AGENT, DAEMON]


def test_one_caller_going_away_does_not_cancel_the_read(stack, monkeypatch):
    monkeypatch.setattr(process_health, "READ_TIMEOUT_S", 0.3)
    stack.answers.update({AGENT: "hang", DAEMON: 200})

    async def scenario():
        leaver = asyncio.ensure_future(stack.health.read())
        stayer = asyncio.ensure_future(stack.health.read())
        await asyncio.sleep(0.05)
        leaver.cancel()
        return await stayer

    assert asyncio.run(scenario()) == {"agent_layer": "unhealthy", "daemon": "running"}


def test_an_answer_is_reused_until_it_is_cache_s_old(stack, monkeypatch):
    stack.answers.update({AGENT: 200, DAEMON: 200})
    now = [1000.0]
    monkeypatch.setattr(process_health.time, "monotonic", lambda: now[0])

    async def twice_then_later():
        first = await stack.health.read()
        stack.answers[DAEMON] = 503
        now[0] += process_health.CACHE_S - 0.1
        cached = await stack.health.read()
        now[0] += 0.2
        fresh = await stack.health.read()
        return first, cached, fresh

    first, cached, fresh = asyncio.run(twice_then_later())
    assert first["daemon"] == cached["daemon"] == "running"
    assert fresh["daemon"] == "unhealthy"
    assert len(stack.seen) == 4


def test_a_real_refused_port_is_not_running_and_no_proxy_is_used(monkeypatch):
    """Both addresses are this install's own: a proxy from the environment would
    answer for a missing process and turn not_running into unhealthy."""
    monkeypatch.setattr(process_health, "get_secret", lambda key: "token")
    monkeypatch.setattr(
        process_health,
        "get_settings",
        lambda: SimpleNamespace(
            agent_url="http://127.0.0.1:1",
            daemon_health_host="127.0.0.1",
            daemon_health_port=1,
        ),
    )

    async def read():
        health = ProcessHealth()
        try:
            assert health._client.trust_env is False
            return await health.read()
        finally:
            await health.aclose()

    assert asyncio.run(read()) == {"agent_layer": "not_running", "daemon": "not_running"}
