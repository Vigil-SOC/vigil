"""http_ready against a local stub server (real sockets) and, for the failures a
laptop can't produce on demand (connect timeout, DNS, TLS), an httpx transport."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from services.medic.sensors import Reading, SensorContext
from services.medic.sensors.http_ready import (
    HttpReady,
    agent_serve_ready,
    agent_worker_ready,
)
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="compose")


@asynccontextmanager
async def stub(
    status: int | None, *, hang: bool = False, drop: bool = False
) -> AsyncIterator[str]:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        if hang:
            await asyncio.sleep(3600)
        if not drop:
            reason = {200: "OK", 401: "Unauthorized", 503: "Service Unavailable"}.get(
                status, "X"
            )
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\ncontent-length: 5\r\n\r\nready".encode()
            )
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/readyz"
    finally:
        server.close()


def sensor(url: str, **kw: object) -> HttpReady:
    return HttpReady(
        "http_ready.agent_worker", "agent-worker", "agent_worker_readyz", url, **kw
    )


def values(reading: Reading) -> dict:
    return {v.key: v.value for v in reading.values}


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_200_is_ready() -> None:
    async with stub(200) as url:
        (r,) = await sensor(url).collect(CTX)
    assert r.error is None
    got = values(r)
    assert (got["ready"], got["result"], got["status_code"]) == (True, "ready", 200)
    assert got["latency_ms"] >= 0
    assert r.instance.startswith("127.0.0.1:")


async def test_503_is_a_result_not_an_error() -> None:
    async with stub(503) as url:
        (r,) = await sensor(url).collect(CTX)
    assert r.error is None
    assert values(r) | {"latency_ms": None} == {
        "ready": False,
        "result": "not_ready",
        "status_code": 503,
        "latency_ms": None,
    }


async def test_connection_refused_is_a_result() -> None:
    (r,) = await sensor(f"http://127.0.0.1:{closed_port()}/readyz").collect(CTX)
    assert r.error is None
    assert values(r) == {
        "ready": False,
        "result": "refused",
        "status_code": None,
        "latency_ms": None,
    }


async def test_no_answer_in_time_is_a_timeout_result() -> None:
    async with stub(200, hang=True) as url:
        (r,) = await sensor(url, timeout_s=0.3).collect(CTX)
    assert r.error is None
    assert values(r)["result"] == "timeout"


async def test_dropped_connection_is_no_response() -> None:
    async with stub(200, drop=True) as url:
        (r,) = await sensor(url).collect(CTX)
    assert values(r)["result"] == "no_response"


async def test_401_is_an_auth_error_for_rules_to_pin_on_redis() -> None:
    async with stub(401) as url:
        (r,) = await sensor(url).collect(CTX)
    assert r.values == ()
    assert (r.error.cls, r.error.http_status) == ("auth", 401)


async def test_404_is_a_wrong_url() -> None:
    async with stub(404) as url:
        (r,) = await sensor(url).collect(CTX)
    assert (r.error.cls, r.error.http_status) == ("http_status", 404)


@pytest.mark.parametrize(
    ("raised", "cls"),
    [
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ConnectError("x"), "other"),
    ],
)
async def test_failures_medic_cannot_get_past_are_errors(
    raised: Exception, cls: str
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise raised

    (r,) = await sensor(
        "http://agent-worker:6990/readyz", transport=httpx.MockTransport(boom)
    ).collect(CTX)
    assert r.error.cls == cls


@pytest.mark.parametrize(
    ("cause", "cls"),
    [(socket.gaierror(8, "nodename"), "dns"), (ssl.SSLError("bad"), "tls")],
)
async def test_dns_and_tls_are_classified(cause: Exception, cls: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x") from cause

    (r,) = await sensor(
        "http://agent-worker:6990/readyz", transport=httpx.MockTransport(boom)
    ).collect(CTX)
    assert r.error.cls == cls


async def test_through_the_framework_every_case_validates() -> None:
    async with stub(200) as ok, stub(503) as down, stub(401) as auth:
        sensors = [
            HttpReady("http_ready.ok", "agent-worker", "agent_worker_readyz", ok),
            HttpReady("http_ready.down", "agent-worker", "agent_worker_readyz", down),
            HttpReady("http_ready.auth", "agent-serve", "agent_serve_readyz", auth),
            HttpReady(
                "http_ready.refused",
                "agent-serve",
                "agent_serve_readyz",
                f"http://127.0.0.1:{closed_port()}/r",
            ),
        ]
        rig = Rig(sensors)
        await rig.scheduler.tick()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(rig.bus) >= 8:
                break
        rig.pipeline.drain()
    rig.assert_all_valid()
    outcome = {o["sensor"]["id"]: o["outcome"] for o in rig.sink.of("sample")}
    assert outcome == {
        "http_ready.ok": "ok",
        "http_ready.down": "ok",
        "http_ready.auth": "error",
        "http_ready.refused": "ok",
    }
    auth = next(o for o in rig.sink.of("sample") if o["outcome"] == "error")
    assert auth["error"] == {"class": "auth", "http_status": 401}


def test_defaults_point_at_the_readiness_ports() -> None:
    assert agent_worker_ready().url == "http://agent-worker:6990/readyz"
    assert agent_serve_ready().url == "http://agent-serve:6989/readyz"
    assert agent_worker_ready().covers == ("agent_worker_readyz",)


def test_instance_never_carries_userinfo() -> None:
    # Review S1: netloc would keep user:password@ and it fits the label pattern.
    s = sensor("http://vigil:hunter2@agent-worker:6990/readyz")
    assert s.instance == "agent-worker:6990"
