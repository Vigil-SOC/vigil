"""gateway_session and the shared gateway client (D6-7)."""

from __future__ import annotations

import ast
import asyncio
import json
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from services.medic.sensors.base import ReadError, SensorContext
from services.medic.sensors.gateway import (
    SESSION_STATES,
    GatewayCall,
    GatewayReply,
    GatewaySession,
    assert_path_subset,
    classify,
    gateway_session,
)
from services.medic.tests.sensors.harness import Rig

CTX = SensorContext(shape="compose")

SESSION_PY = Path(__file__).resolve().parents[3] / "medic_gateway" / "session.py"
HERE = Path(__file__).resolve()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _transport(status: int, body: bytes, content_type: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/_gw/status"
        return httpx.Response(
            status, content=body, headers={"content-type": content_type}
        )

    return httpx.MockTransport(handler)


def _session(status: int, body: bytes, content_type: str) -> GatewaySession:
    return GatewaySession(
        "medic-gateway-out",
        8471,
        transport=_transport(status, body, content_type),
    )


def _problem(status: int, code: str) -> GatewaySession:
    body = json.dumps({"status": status, "code": code}).encode()
    return _session(status, body, "application/problem+json")


def _string_leaves(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _string_leaves(node.body) | _string_leaves(node.orelse)
    return set()


def _viewer_states(source: str) -> set[str]:
    """ViewerSession's states, from the text of session.py. Not an import."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for elt, value in zip(target.elts, node.value.elts, strict=True):
                    if (
                        isinstance(elt, ast.Attribute)
                        and elt.attr == "state"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        found.add(value.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_set"
            and node.args
        ):
            found |= _string_leaves(node.args[0])
    return found


def test_the_enum_is_viewersessions_states() -> None:
    assert set(SESSION_STATES) == _viewer_states(SESSION_PY.read_text())


def test_the_client_does_not_import_the_gateway_package() -> None:
    text = HERE.parents[2].joinpath("sensors/gateway.py").read_text()
    assert "import services.medic_gateway" not in text
    assert "from services.medic_gateway" not in text


def test_declared_outbound_calls_match_the_list() -> None:
    assert_path_subset(
        (
            GatewayCall("/api/health"),
            GatewayCall("/api/v1/findings", query=("data_source",)),
            GatewayCall("/api/v1/agent-runs", query=("status", "limit")),
        )
    )


def test_a_path_not_on_the_list_fails() -> None:
    with pytest.raises(AssertionError, match="not on OUTBOUND"):
        assert_path_subset((GatewayCall("/api/not-a-route"),))


def test_a_query_not_on_the_list_fails() -> None:
    with pytest.raises(AssertionError, match="query not on OUTBOUND"):
        assert_path_subset((GatewayCall("/api/health", query=("token",)),))


def test_a_call_that_omits_a_required_query_fails() -> None:
    with pytest.raises(AssertionError, match="omits query OUTBOUND requires"):
        assert_path_subset((GatewayCall("/api/v1/findings"),))


def test_status_is_not_an_outbound_call() -> None:
    with pytest.raises(AssertionError, match="own route"):
        assert_path_subset((GatewayCall("/_gw/status"),))


@pytest.mark.parametrize(
    ("status", "code", "cls", "detail"),
    [
        (502, "auth_rejected", "auth", "auth_rejected"),
        (502, "auth_locked", "auth", "auth_locked"),
        (502, "auth_rejected_after_login", "auth", "auth_rejected_after_login"),
        (502, "upstream_unreachable", "http_status", "upstream_unreachable"),
        (502, "upstream_redirect", "http_status", "upstream_redirect"),
        (504, "upstream_timeout", "timeout", "upstream_timeout"),
        (503, "busy", "other", "busy"),
    ],
)
def test_the_gateways_own_failures_are_errors(
    status: int, code: str, cls: str, detail: str
) -> None:
    body = json.dumps({"status": status, "code": code}).encode()
    got = classify(status, "application/problem+json", body)
    assert isinstance(got, ReadError)
    assert (got.cls, got.http_status, got.detail) == (cls, status, detail)


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/problem+json"],
)
def test_a_relayed_backend_5xx_is_a_result(content_type: str) -> None:
    # The gateway relays both types. problem+json is not, by itself, its own error.
    body = b'{"status":500,"title":"Server Error"}'
    got = classify(500, content_type, body)
    assert isinstance(got, GatewayReply)
    assert got.status == 500 and got.body == {"status": 500, "title": "Server Error"}


def test_a_401_is_an_auth_error_not_a_result() -> None:
    got = classify(401, "application/json", b'{"detail":"no"}')
    assert isinstance(got, ReadError)
    assert (got.cls, got.http_status) == ("auth", 401)
    assert got.detail is None


async def test_each_session_state_validates() -> None:
    sensors = []
    for state in SESSION_STATES:
        body = json.dumps({"state": state, "since": 1}).encode()
        sensors.append(
            GatewaySession(
                "medic-gateway-out",
                8471,
                id=f"gateway_session.{state}",
                transport=_transport(200, body, "application/json"),
            )
        )
    rig = Rig(sensors)
    await rig.tick()
    rig.assert_all_valid()
    samples = [o for o in rig.sink.of("sample") if o["signal"] == "gateway_session"]
    assert {o["values"][0]["value"] for o in samples} == set(SESSION_STATES)
    assert {o["outcome"] for o in samples} == {"ok"}
    assert {o["target"]["service"] for o in samples} == {"medic-gateway"}


async def test_dev_mode_reports_gateway_session_off() -> None:
    sensor = gateway_session("medic-gateway-out", 8471)
    rig = Rig([sensor], dev_mode=True)
    await rig.tick()
    assert rig.scheduler.status()["off"] == {"gateway_session": "vigil_dev_mode"}
    assert rig.sink.of("sample") == []


async def test_auth_rejection_is_an_error_through_the_framework() -> None:
    rig = Rig([_problem(502, "auth_bad_password")])
    await rig.tick()
    rig.assert_all_valid()
    (sample,) = [o for o in rig.sink.of("sample") if o["outcome"] == "error"]
    assert sample["error"] == {
        "class": "auth",
        "http_status": 502,
        "detail": "auth_bad_password",
    }


async def test_a_non_200_on_status_is_not_a_session_reading() -> None:
    sensor = _session(500, b'{"detail":"backend"}', "application/json")
    (reading,) = await sensor.collect(CTX)
    assert reading.error is not None
    assert (reading.error.cls, reading.error.http_status) == ("http_status", 500)


async def test_an_unknown_state_is_a_parse_error() -> None:
    sensor = _session(200, b'{"state":"elsewhere"}', "application/json")
    (reading,) = await sensor.collect(CTX)
    assert reading.error is not None and reading.error.cls == "parse"
    assert reading.values == ()


async def test_connection_refused_is_an_error() -> None:
    sensor = GatewaySession("127.0.0.1", _closed_port())
    (reading,) = await sensor.collect(CTX)
    assert reading.error is not None and reading.error.cls == "refused"


@asynccontextmanager
async def _listen(status: int, body: bytes, content_type: str) -> AsyncIterator[int]:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        payload = (
            f"HTTP/1.1 {status} X\r\ncontent-type: {content_type}\r\n"
            f"content-length: {len(body)}\r\n\r\n"
        ).encode() + body
        writer.write(payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()


async def test_a_real_status_reply_is_the_state() -> None:
    body = b'{"state":"ok","since":4}'
    async with _listen(200, body, "application/json") as port:
        sensor = GatewaySession("127.0.0.1", port)
        (reading,) = await sensor.collect(CTX)
    assert reading.error is None
    assert [(v.key, v.value) for v in reading.values] == [("state", "ok")]
    assert reading.instance == f"127.0.0.1:{port}"


@pytest.mark.parametrize(
    ("cause", "cls"),
    [(socket.gaierror(8, "nodename"), "dns"), (ssl.SSLError("bad"), "tls")],
)
async def test_dns_and_tls_match_a_failed_connect(cause: Exception, cls: str) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x") from cause

    sensor = GatewaySession(
        "medic-gateway-out", 8471, transport=httpx.MockTransport(boom)
    )
    (reading,) = await sensor.collect(CTX)
    assert reading.error is not None and reading.error.cls == cls
