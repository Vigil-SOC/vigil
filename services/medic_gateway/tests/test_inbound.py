"""backend -> gateway -> Medic API (C5 #7, C3 check 11): only X2's reads plus the one
feedback POST, only the X-Medic-* headers, and the two lists never mix."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from services.medic_gateway import policy
from services.medic_gateway.tests.cases import CASES, wire
from services.medic_gateway.tests.conftest import KEY, inbound, keyed, send

OPENAPI = Path(__file__).resolve().parents[2] / "medic/contracts/medic-api.openapi.yaml"

INBOUND_OK = [
    (b"GET", b"/v1/status"),
    (b"GET", b"/v1/incidents"),
    (b"GET", b"/v1/incidents?state=open&limit=50"),
    (
        b"GET",
        b"/v1/incidents?lane=2&subject=watcher&unrated=true&include_suppressed=false",
    ),
    (b"GET", b"/v1/incidents?opened_after=2026-10-01T00:00:00.123Z&cursor=abc_-9"),
    (b"GET", b"/v1/incidents/inc_abcdef12"),
    (b"GET", b"/v1/decisions?type=feedback&type=anchor&from_seq=0&limit=100"),
    (b"GET", b"/v1/decisions?incident_id=inc_abcdef12"),
    (b"GET", b"/v1/decisions/42"),
    (b"GET", b"/v1/sensors"),
    (b"GET", b"/v1/pack"),
    (b"GET", b"/v1/export?from=2026-10-01T00:00:00Z&to=2026-10-08T00:00:00Z"),
]


@pytest.mark.parametrize("method,target", INBOUND_OK)
def test_inbound_reads_pass(env, method, target):
    status, hdrs, _ = inbound(env, method, target, keyed())
    assert status == 200 and hdrs.get("x-medic-api-version") == "1.0"
    assert env.medic.seen[0]["target"] == target.decode()


def test_inbound_feedback_post_passes_only_medic_headers(env):
    body = b'{"value":"agree"}'
    extra = (
        b"X-Medic-Admin: user:42\r\nX-Request-Id: r-1\r\n"
        b"Cookie: session=x\r\nAuthorization: Bearer y\r\nX-Forwarded-For: 1.1.1.1\r\n"
        b"User-Agent: browser\r\nX-CSRF-Token: z\r\n"
    )
    status, _, _ = inbound(
        env, b"POST", b"/v1/incidents/inc_abcdef12/feedback", keyed(extra), body
    )
    assert status == 200
    (got,) = env.medic.seen
    sent = {k.lower(): v for k, v in got["headers"].items()}
    assert set(sent) == {
        "host",
        "user-agent",
        "accept",
        "connection",
        "content-length",
        "content-type",
        "x-medic-key",
        "x-medic-admin",
        "x-request-id",
    }
    assert sent["x-medic-key"] == KEY and sent["x-medic-admin"] == "user:42"
    assert got["method"] == "POST" and got["body"] == body


def test_export_headers_come_back(env):
    env.medic.extra = {
        "X-Medic-Sha256": "a" * 64,
        "Content-Disposition": 'attachment; filename="medic-evidence-x.json"',
        "X-Internal": "1",
    }
    target = b"/v1/export?from=2026-10-01T00:00:00Z&to=2026-10-08T00:00:00Z"
    status, hdrs, _ = inbound(env, b"GET", target, keyed())
    assert status == 200 and hdrs["x-medic-sha256"] == "a" * 64
    assert "content-disposition" in hdrs and "x-internal" not in hdrs


def test_inbound_body_up_to_8_mib(env):
    env.medic.body = b'"' + b"a" * (6 << 20) + b'"'
    assert inbound(env, b"GET", b"/v1/pack", keyed())[0] == 200


@pytest.mark.parametrize(
    "method,target,status",
    [
        (b"POST", b"/v1/incidents/inc_abcdef12", 405),
        (b"POST", b"/v1/pack", 405),
        (b"POST", b"/v1/status", 405),
        (b"PUT", b"/v1/incidents/inc_abcdef12/feedback", 405),
        (b"PATCH", b"/v1/incidents/inc_abcdef12/feedback", 405),
        (b"DELETE", b"/v1/decisions/1", 405),
        (b"GET", b"/v1/incidents/inc_abcdef12/feedback", 405),
        (b"HEAD", b"/v1/status", 405),
        (b"OPTIONS", b"/v1/status", 405),
        (b"GET", b"/v1/incidents/../status", 400),
        (b"GET", b"/v1/incidents/inc_x%2f", 400),
        (b"GET", b"/v1//status", 400),
        (b"GET", b"/v1/status/", 400),
        (b"GET", b"/v1/incidents/not-an-id", 403),
        (b"GET", b"/v1/incidents/inc_short", 403),
        (b"GET", b"/v2/status", 403),
        (b"GET", b"/V1/status", 403),
        (b"GET", b"/v1/decisions?type=a&type=b", 400),
        (b"GET", b"/v1/decisions?" + b"&".join([b"type=anchor"] * 9), 400),
        (b"GET", b"/v1/incidents?limit=1&limit=2", 400),
        (b"GET", b"/v1/incidents?limit=201", 400),
        (b"GET", b"/v1/decisions?limit=101", 400),
        (b"GET", b"/v1/incidents?limit=0", 400),
        (b"GET", b"/v1/incidents?lane=4", 400),
        (b"GET", b"/v1/incidents?opened_after=2026-10-01", 400),
        (b"GET", b"/v1/status?x=1", 400),
        (b"GET", b"/api/health", 403),
        (b"POST", b"/v1/pack/import", 403),
        (b"POST", b"/api/auth/password-reset/confirm", 403),
        (b"GET", b"http://medic/v1/status", 400),
    ],
)
def test_inbound_everything_else_refused(env, method, target, status):
    body = b"{}" if method in (b"POST", b"PUT", b"PATCH") else b""
    got, _, _ = inbound(env, method, target, keyed(), body)
    assert got == status
    assert env.medic.seen == []


@pytest.mark.parametrize(
    "extra,body,status",
    [
        (b"", b"x" * 4097, 413),
        (b"", b"", 400),  # no Content-Length
        (b"Transfer-Encoding: chunked\r\n", b"{}", 400),
        (b"X-Medic-Admin: admin\r\n", b"{}", 400),
        (b"X-Medic-Admin: user:" + b"a" * 124 + b"\r\n", b"{}", 400),
        (b"X-HTTP-Method-Override: DELETE\r\n", b"{}", 400),
    ],
)
def test_feedback_post_framing_is_strict(env, extra, body, status):
    target = b"/v1/incidents/inc_abcdef12/feedback"
    raw = b"POST " + target + b" HTTP/1.1\r\nHost: g\r\n" + keyed(extra)
    raw += b"Connection: close\r\nContent-Type: application/json\r\n"
    if body:
        raw += b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    got, _, _ = send(env.inn, raw + b"\r\n" + body)
    assert got == status
    assert env.medic.seen == []


def test_feedback_post_must_be_json(env):
    target = b"/v1/incidents/inc_abcdef12/feedback"
    raw = b"POST " + target + b" HTTP/1.1\r\nHost: g\r\n" + keyed()
    raw += b"Content-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}"
    assert send(env.inn, raw)[0] == 415
    assert env.medic.seen == []


@pytest.mark.parametrize("key", [None, b"short", b"k" * 44, b"k" * 42 + b"="])
def test_inbound_requires_medic_key_shape(env, key):
    headers = b"" if key is None else b"X-Medic-Key: " + key + b"\r\n"
    assert inbound(env, b"GET", b"/v1/status", headers)[0] == 401
    assert env.medic.seen == []


def test_lists_are_separate(env):
    assert send(env.inn, wire(CASES[0]))[0] == 403  # /api/health on the inbound side
    assert inbound(env, b"GET", b"/v1/status", keyed())[0] == 200
    raw = b"GET /v1/status HTTP/1.1\r\nHost: g\r\n" + keyed() + b"\r\n"
    assert send(env.out, raw)[0] == 403
    assert env.backend.seen == []


def test_inbound_sends_no_viewer_token(env):
    inbound(env, b"GET", b"/v1/status", keyed())
    assert "Authorization" not in env.medic.seen[0]["headers"]
    assert env.backend.seen == [], "the inbound side never logs in"


# ----------------------------------------------------------------------------- X2 contract
def _ops() -> dict:
    spec = yaml.safe_load(OPENAPI.read_text())
    params = spec["components"].get("parameters", {})
    record = yaml.safe_load(
        (OPENAPI.parent / "decision-record.schema.json").read_text()
    )
    ops = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method == "parameters":
                continue
            ps = [
                params[p["$ref"].split("/")[-1]] if "$ref" in p else p
                for p in item.get("parameters", []) + op.get("parameters", [])
            ]
            ops[(method.upper(), path)] = (ps, record, spec)
    return ops


def _schema(schema: dict, record: dict, spec: dict) -> dict:
    ref = schema.get("$ref", "")
    if ref.startswith("./decision-record.schema.json#/"):
        node = record
        for part in ref.split("#/")[1].split("/"):
            node = node[part]
        return node
    if ref.startswith("#/components/schemas/"):
        return spec["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return schema


def _accepts(regex: str, value) -> bool:
    return (
        re.fullmatch(
            regex, str(value).lower() if isinstance(value, bool) else str(value)
        )
        is not None
    )


def test_inbound_list_is_exactly_the_x2_operations():
    ops = _ops()
    assert {(r.method, r.path) for r in policy.INBOUND} == set(ops)
    posts = [r for r in policy.INBOUND if r.method != "GET"]
    assert [(r.method, r.path) for r in posts] == [
        ("POST", "/v1/incidents/{incident_id}/feedback")
    ]


@pytest.mark.parametrize("route", policy.INBOUND, ids=lambda r: f"{r.method} {r.path}")
def test_inbound_parameters_match_the_x2_spec(route):
    ps, record, spec = _ops()[(route.method, route.path)]
    query = {
        p["name"]: _schema(p["schema"], record, spec) for p in ps if p["in"] == "query"
    }
    segs = {
        p["name"]: _schema(p["schema"], record, spec) for p in ps if p["in"] == "path"
    }
    assert set(route.query) == set(query)
    assert set(route.segs) == set(segs)
    for name, schema in {**query, **segs}.items():
        regex = route.query[name][0] if name in route.query else route.segs[name]
        repeats = route.query[name][1] if name in route.query else 1
        if schema.get("type") == "array":
            assert repeats == schema["maxItems"]
            schema = _schema(schema["items"], record, spec)
        else:
            assert repeats == 1
        if "enum" in schema:
            assert all(_accepts(regex, v) for v in schema["enum"])
            assert not _accepts(regex, "x")
        elif schema.get("type") == "boolean":
            assert (
                _accepts(regex, True)
                and _accepts(regex, False)
                and not _accepts(regex, 1)
            )
        elif schema.get("type") == "integer":
            lo = schema["minimum"]
            assert _accepts(regex, lo) and not _accepts(regex, lo - 1)
            if "maximum" in schema:
                hi = schema["maximum"]
                assert _accepts(regex, hi) and not _accepts(regex, hi + 1)
            else:  # unbounded in the spec: any non-negative 64-bit value
                assert _accepts(regex, 2**63 - 1) and not _accepts(regex, "01")
        else:
            assert regex == schema["pattern"].removeprefix("^").removesuffix("$")
