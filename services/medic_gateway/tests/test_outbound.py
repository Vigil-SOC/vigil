"""Medic -> gateway -> backend: C3 check 10 (the whole SP1 bypass table) and the
header, response and limit rules around it."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from services.medic_gateway import policy, server
from services.medic_gateway.tests.cases import CASES, wire
from services.medic_gateway.tests.conftest import get, send


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_bypass_case(env, case):
    host = b"evil.example" if case.id == "host-evil" else b"gateway"
    status, _, body = send(env.out, wire(case, host=host))
    action, want = case.gateway
    if action == "reject":
        assert status == want, body
        assert env.backend.data() == [], "a rejected request reached the backend"
    else:
        assert status == 200, body
        (got,) = env.backend.data()
        assert got["target"] == want
        assert got["headers"]["Host"] == f"127.0.0.1:{env.backend.port}"


def test_outbound_list_is_exactly_sp1_decision_1a_prime():
    assert sorted(r.path for r in policy.OUTBOUND) == sorted(
        [
            "/api/health",
            "/api/health/ready",
            "/api/federation/sources",
            "/api/federation/health",
            "/api/triage",
            "/api/orchestrator/status",
            "/api/v1/findings",
            "/api/v1/approvals/pending",
            "/api/v1/approvals",
            "/api/v1/agent-runs",
            "/api/kafka/status",
            "/api/llm/providers",
            "/api/services/ollama/status",
            "/api/webhooks/darktrace/health",
            "/api/webhooks/cloudflare/cloudy/health",
        ]
    )
    assert {r.method for r in policy.OUTBOUND} == {"GET"}


def test_every_listed_path_forwards(env):
    for r in policy.OUTBOUND:
        # The first alternative of each required parameter's regex, e.g. "probe".
        query = "&".join(f"{n}={r.query[n][0].split('|')[0]}" for n in r.required)
        target = r.path + ("?" + query if query else "")
        assert get(env, target.encode()) == 200, target
    assert len(env.backend.data()) == len(policy.OUTBOUND)


def test_auth_routes_can_never_be_listed():
    bad = [policy.Route("GET", "/api/auth/me")]
    with pytest.raises(ValueError):
        server.make_server(
            "outbound", ("127.0.0.1", 0), server.Upstream("x", 1), routes=bad
        )


def test_context_path_is_added_by_the_gateway(env):
    env.out_srv.RequestHandlerClass.upstream = server.Upstream(
        "127.0.0.1", env.backend.port, prefix="/vigil"
    )
    assert get(env, b"/api/health") == 200
    assert env.backend.targets() == ["/vigil/api/health"]


# ----------------------------------------------------------------------------- headers
def test_caller_credentials_and_forwarding_headers_are_stripped(env):
    raw = (
        b"GET /api/federation/sources HTTP/1.1\r\nHost: gateway\r\n"
        b"Authorization: Bearer stolen\r\nCookie: access_token=stolen\r\n"
        b"X-Forwarded-For: 10.0.0.1\r\nX-Forwarded-Host: evil\r\n"
        b"X-Forwarded-Proto: https\r\nForwarded: for=1.2.3.4\r\n"
        b"X-Real-IP: 1.2.3.4\r\nUser-Agent: medic-evil\r\nX-CSRF-Token: a\r\n"
        b"Upgrade: h2c\r\nTE: trailers\r\nKeep-Alive: 5\r\nProxy-Authorization: x\r\n"
        b"X-Medic-Key: " + b"k" * 43 + b"\r\nX-Request-Id: req-1\r\n"
        b"Connection: close, X-Secret\r\nX-Secret: 1\r\n\r\n"
    )
    status, hdrs, _ = send(env.out, raw)
    assert status == 200
    (got,) = env.backend.data()
    sent = {k.lower(): v for k, v in got["headers"].items()}
    assert set(sent) == {
        "host",
        "user-agent",
        "accept",
        "connection",
        "authorization",
        "x-request-id",
    }
    assert sent["user-agent"] == server.UA
    assert sent["authorization"] != "Bearer stolen"
    assert "set-cookie" not in hdrs, "upstream Set-Cookie must not reach Medic"


def test_bad_request_id_is_dropped_not_forwarded(env):
    raw = b"GET /api/health HTTP/1.1\r\nHost: g\r\nX-Request-Id: a b\r\n\r\n"
    assert send(env.out, raw)[0] == 200
    assert "X-Request-Id" not in env.backend.data()[0]["headers"]


def test_only_allowlisted_response_headers_come_back(env):
    env.backend.extra = {"Retry-After": "5", "X-Internal": "1", "Location": "/x"}
    status, hdrs, _ = send(env.out, wire(CASES[0]))
    assert status == 200
    assert hdrs["retry-after"] == "5"
    assert not {"x-internal", "location", "set-cookie"} & set(hdrs)


# ----------------------------------------------------------------------------- responses
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_backend_redirect_is_not_followed_or_passed(env, code):
    env.backend.status = code
    status, hdrs, _ = send(env.out, wire(CASES[0]))
    assert status == 502 and "location" not in hdrs
    assert len(env.backend.data()) == 1


def test_non_json_upstream_body_is_refused(env):
    env.backend.content_type = "text/html"
    env.backend.body = b"<html>"
    assert send(env.out, wire(CASES[0]))[0] == 502


def test_oversized_upstream_body_is_refused(env):
    env.backend.body = b'"' + b"a" * (2 * 1024 * 1024) + b'"'
    status, _, body = send(env.out, wire(CASES[0]))
    assert status == 502 and len(body) < 1024


def test_slow_upstream_hits_the_total_deadline(env):
    env.out_srv.RequestHandlerClass.upstream = server.Upstream(
        "127.0.0.1", env.backend.port, timeout=0.5
    )
    env.backend.body = b'"' + b"a" * 40 + b'"'
    env.backend.drip = 0.05  # each byte arrives well inside a socket timeout
    t0 = time.monotonic()
    status, _, _ = send(env.out, wire(CASES[0]))
    assert status == 504
    assert time.monotonic() - t0 < 1.5


def test_unreachable_backend_is_502(env):
    port = env.backend.port
    env.backend.srv.shutdown()
    env.backend.srv.server_close()
    env.out_srv.RequestHandlerClass.upstream = server.Upstream("127.0.0.1", port)
    assert get(env, b"/api/health") == 502


def test_slow_client_is_cut_off(env):
    env.out_srv.RequestHandlerClass.timeout = 0.3
    s = socket.create_connection(("127.0.0.1", env.out), timeout=5)
    s.sendall(b"GET /api/health HTTP/1.1\r\nHost: g\r\n")  # never finishes
    t0 = time.monotonic()
    assert s.recv(1024) == b"", "the gateway closes the connection"
    assert time.monotonic() - t0 < 2
    s.close()
    assert env.backend.data() == []


def test_busy_gateway_sheds_load(env):
    env.out_srv.RequestHandlerClass.slots = threading.BoundedSemaphore(1)
    env.out_srv.RequestHandlerClass.slots.acquire()
    assert get(env, b"/api/health") == 503
    assert env.backend.data() == []


def test_gateway_status_reports_auth_state(env):
    env.backend.login_status = 423
    get(env)
    status, hdrs, body = send(env.out, b"GET /_gw/status HTTP/1.1\r\nHost: g\r\n\r\n")
    assert status == 200 and hdrs["content-type"] == "application/json"
    assert json.loads(body)["state"] == "locked"


def test_gateway_status_is_outbound_only(env):
    status, _, _ = send(env.inn, b"GET /_gw/status HTTP/1.1\r\nHost: g\r\n\r\n")
    assert status in (401, 403)
    assert env.medic.seen == []
