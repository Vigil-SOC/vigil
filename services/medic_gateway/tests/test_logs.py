"""Structured logs through a redacting filter: no password, token, key or body, ever.

Medic reads the gateway's logs (C3 §4.6, last row), so a canary that shows up here
would end up in Medic's store.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from services.medic_gateway import logs
from services.medic_gateway.tests.conftest import KEY, PASSWORD, get, inbound, keyed

BODY_CANARY = "body-canary-7f3e"


@pytest.fixture
def stream():
    buf = io.StringIO()
    handler = logs.setup(buf)
    yield buf
    logging.getLogger(logs.LOGGER).removeHandler(handler)


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(ln) for ln in buf.getvalue().splitlines()]


def test_lines_are_json_with_fixed_fields(env, stream):
    get(env, b"/api/health")
    (line,) = [ln for ln in lines(stream) if ln["msg"] == "request"]
    assert {
        "ts",
        "level",
        "logger",
        "msg",
        "dir",
        "method",
        "status",
        "code",
        "ms",
    } <= set(line)
    assert line["logger"] == "services.medic_gateway" and line["route"] == "/api/health"


def test_refused_target_is_not_logged(env, stream):
    get(env, b"/api/x-" + BODY_CANARY.encode())
    assert BODY_CANARY not in stream.getvalue()
    assert lines(stream)[-1]["code"] == "not_on_list"


def test_no_credential_or_body_in_any_log_line(env, stream):
    tokens: set[str] = set()
    env.backend.body = json.dumps({"x": BODY_CANARY}).encode()
    env.medic.body = env.backend.body
    get(env)  # login
    tokens |= {env.session.access, env.session.refresh_tok}
    env.backend.reject_tokens.add(env.session.access)
    env.clock.offset = 60
    get(env)  # 401 -> refresh -> retry
    tokens |= {env.session.access, env.session.refresh_tok}
    inbound(
        env,
        b"POST",
        b"/v1/incidents/inc_abcdef12/feedback",
        keyed(),
        json.dumps({"comment": BODY_CANARY}).encode(),
    )
    env.backend.login_status = 401
    env.session.access = env.session.refresh_tok = None
    get(env)  # bad password
    # Direct attempts: the filter is the last line of defence, not the call sites.
    log = logging.getLogger(logs.LOGGER)
    log.info("leak %s %s", PASSWORD, min(tokens))
    logs.event("leak", password=PASSWORD, token=max(tokens), nested={"t": PASSWORD})
    try:
        raise RuntimeError(PASSWORD)
    except RuntimeError:
        log.exception("boom %s", PASSWORD)
    logging.getLogger(logs.LOGGER + ".child").warning("child %s", PASSWORD)
    text = stream.getvalue()
    assert len(lines(stream)) >= 6
    assert PASSWORD not in text
    assert "canary-PW" not in text
    assert KEY not in text
    assert BODY_CANARY not in text
    assert "eyJ" not in text
    for t in tokens:
        assert t not in text and t.split(".")[-1] not in text


def test_redact_scrubs_jwt_shapes_without_knowing_them():
    assert logs.redact("x eyJhbGciOi.eyJleHAiOjF9.c2ln y") == "x [jwt] y"
    assert logs.redact("Bearer abc.def") == "Bearer [redacted]"
