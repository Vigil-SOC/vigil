"""C3 §7 check 4 and the bypass shapes, against a fake Docker (runs everywhere).

A refused request must never reach Docker: every refusal case also asserts the
fake socket saw nothing. The same table runs against a live Docker in
test_live_docker.py (SP2's 43).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from services.medic_dockerproxy.tests.conftest import CID, body_of

ALLOWED = [
    "/_ping",
    "/v1.54/_ping",
    "/containers/json",
    "/v1.54/containers/json",
    "/containers/json?all=1&limit=5&filters=%7B%7D",
    f"/containers/{CID}/json",
    f"/v1.43/containers/{CID}/json",
    (
        f"/containers/{CID}/logs?follow=1&stdout=1&stderr=1&timestamps=1"
        "&since=1791430868.123456789&tail=all"
    ),
    f"/containers/{CID}/logs?until=1791430868&tail=100",
    "/events?since=1791430868&until=1791430999",
    "/v1.54/events",
]


@pytest.mark.parametrize("target", ALLOWED)
def test_allowed(h, target):
    status, _ = h.get(target)
    assert status == 200
    assert len(h.docker.heads) == 1


# (method, target, extra headers). SP2's 32 refusals first, then the gateway's
# ambiguity shapes (C3 check 10 style) applied to the Docker paths.
REFUSED = [
    ("POST", f"/containers/{CID}/restart", ""),
    ("POST", f"/containers/{CID}/kill", ""),
    ("GET", f"/containers/{CID}/archive?path=/etc/passwd", ""),
    ("GET", f"/containers/{CID}/export", ""),
    ("GET", f"/containers/{CID}/attach/ws?stdin=1&stream=1", ""),
    ("POST", f"/containers/{CID}/attach?stdin=1&stream=1", ""),
    ("GET", f"/containers/{CID}/top", ""),
    ("GET", f"/containers/{CID}/stats", ""),
    ("GET", f"/containers/{CID}/changes", ""),
    ("POST", f"/containers/{CID}/exec", ""),
    ("GET", "/images/json", ""),
    ("GET", "/info", ""),
    ("GET", "/version", ""),
    ("GET", "/secrets", ""),
    ("HEAD", "/containers/json", ""),
    ("OPTIONS", "/containers/json", ""),
    ("DELETE", f"/containers/{CID}", ""),
    ("GET", "/containers/json", "X-HTTP-Method-Override: POST\r\n"),
    ("GET", f"/containers/{CID}/logs", "Upgrade: tcp\r\nConnection: Upgrade\r\n"),
    ("GET", "/containers/json", "Content-Length: 5\r\n"),
    ("GET", "/containers/json", "Transfer-Encoding: chunked\r\n"),
    ("GET", f"/containers/{CID}/logs/../archive?path=/", ""),
    ("GET", f"/containers/{CID}%2Farchive?path=/", ""),
    ("GET", f"/containers/{CID}/%2e%2e/x/archive", ""),
    ("GET", "//containers/json", ""),
    ("GET", f"/v1.54//containers/{CID}/export", ""),
    ("GET", f"/containers/{CID}/json/../export", ""),
    ("GET", f"/containers/{CID}/logs?follow=1&details=1", ""),
    ("GET", f"/containers/{CID}/logs?since=1&since=2", ""),
    ("GET", f"/containers/{CID}/logs;x=1", ""),
    ("GET", f"http://docker/containers/{CID}/export", ""),
    ("GET", f"/containers/{CID}/json?size=1", ""),
    # Ambiguity shapes beyond SP2 (matched raw, refused, never normalised)
    ("GET", "/containers/json/", ""),
    ("GET", "/containers/./json", ""),
    ("GET", "/v1.54/../containers/json", ""),
    ("GET", "/containers\\json", ""),
    ("GET", "/containers/json#frag", ""),
    ("GET", "/containers/json?all=1#frag", ""),
    ("GET", "/Containers/json", ""),
    ("GET", "/v2.0/containers/json", ""),
    ("GET", "/v1.54/v1.54/containers/json", ""),
    ("GET", "/containers/%6Aon", ""),
    ("GET", "/containers/json+", ""),
    ("GET", "*", ""),
    ("GET", "docker:2375", ""),
    ("GET", f"/containers/-{CID}/json", ""),
    ("GET", f"/containers/{'a' * 129}/json", ""),
    ("GET", "/containers/json?all=yes", ""),
    ("GET", "/containers/json?all", ""),
    ("GET", "/containers/json?size=1", ""),
    ("GET", "/containers/json?filters=%5B%5D", ""),  # filters must be an object
    ("GET", "/containers/json?filters=notjson", ""),
    ("GET", "/containers/json?all=1&&limit=1", ""),
    ("GET", "/containers/json?all=1;limit=1", ""),
    ("GET", f"/containers/{CID}/logs?since=yesterday", ""),
    ("GET", f"/containers/{CID}/logs?tail=-1", ""),
    ("GET", f"/containers/{CID}/logs?follow=1%26details%3D1", ""),
    ("GET", "/events?filters=%7B%22type%22%3A%22image%22%7D&filters=%7B%7D", ""),
    ("GET", "/events?filters=%7B%22event%22%3A%5B%22exec_create%22%5D%7D", ""),
    (
        "GET",
        "/events?filters=%7B%22event%22%3A%5B%22start%22%2C%22exec_start%22%5D%7D",
        "",
    ),
    ("GET", "/_ping?x=1", ""),
    ("GET", "/containers/json", "Content-Length: 0\r\nContent-Length: 0\r\n"),
    ("GET", "/containers/json", "Host: other\r\n"),
    ("GET", "/containers/json", "X-HTTP-Method: DELETE\r\n"),
    ("GET", "/containers/json", "X-Method-Override: POST\r\n"),
    ("GET", "/containers/json", "Expect: 100-continue\r\n"),
    ("GET", "/containers/json", "bad header line\r\n"),
    ("PUT", f"/containers/{CID}/archive?path=/", ""),
    ("PATCH", "/containers/json", ""),
    ("CONNECT", "docker:2375", ""),
]


@pytest.mark.parametrize("method,target,extra", REFUSED)
def test_refused(h, method, target, extra):
    status, resp = h.get(target, extra, method)
    assert status in (400, 403, 405), resp[:200]
    assert b"medic-dockerproxy" in resp
    assert h.docker.heads == [], "a refused request reached Docker"


@pytest.mark.parametrize(
    "line",
    [
        b"GET /containers/json HTTP/1.0\r\n\r\n",
        b"GET /containers/json HTTP/2\r\n\r\n",
        b"GET  /containers/json HTTP/1.1\r\n\r\n",
        b"GET /containers/json\r\n\r\n",
        b"GET /containers/json HTTP/1.1 x\r\n\r\n",
        "GET /containers/jéson HTTP/1.1\r\n\r\n".encode(),
        b"GET /containers/json HTTP/1.1\r\nX: \x00\r\n\r\n",
        b"GET /containers/json HTTP/1.1\nHost: x\n\n",
    ],
)
def test_malformed_request_line(h, line):
    status, resp = h.raw(line, timeout=2)
    assert status in (0, 400, 403, 405), resp[:200]
    assert h.docker.heads == []


def test_oversized_head_refused(h):
    status, _ = h.raw(b"GET /_ping HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n")
    assert status in (0, 400, 431)
    assert h.docker.heads == []


def test_upstream_request_is_resynthesised(h):
    """Client headers never reach Docker; the target is rebuilt from parsed values."""
    f = quote(json.dumps({"label": ["com.docker.compose.project=vigil"]}))
    h.get(
        f"/v1.54/containers/json?all=true&filters={f}",
        "Authorization: Bearer x\r\nCookie: a=b\r\nX-Registry-Auth: y\r\n",
    )
    (head,) = h.docker.heads
    lines = head.decode().split("\r\n")
    target = lines[0].split(" ")[1]
    assert lines[0].startswith("GET /v1.54/containers/json?") and lines[0].endswith(
        " HTTP/1.1"
    )
    assert {x.split(":")[0].lower() for x in lines[1:] if x} == {"host", "connection"}
    q = parse_qs(urlsplit(target).query, strict_parsing=True)
    assert q["all"] == ["true"]
    assert json.loads(q["filters"][0]) == {
        "label": ["com.docker.compose.project=vigil"]
    }


@pytest.mark.parametrize(
    "filters",
    [None, {}, {"type": ["image", "network"]}, {"type": ["plugin"], "label": ["a=b"]}],
)
def test_events_type_forced_to_container(h, filters):
    target = "/events?since=1"
    if filters is not None:
        target += "&filters=" + quote(json.dumps(filters))
    assert h.get(target)[0] == 200
    (head,) = h.docker.heads
    sent = parse_qs(urlsplit(head.split(b" ")[1].decode()).query)
    got = json.loads(sent["filters"][0])
    assert got["type"] == ["container"]
    if filters and "label" in filters:
        assert got["label"] == ["a=b"]
    # S5p-7: exec_* events carry whole command lines (SP2 ⚑1's leak, via events).
    assert set(got["event"]) == {
        "create", "start", "restart", "die", "kill", "stop", "oom", "destroy",
        "health_status",
    }  # fmt: skip


def test_events_may_narrow_the_lifecycle_set(h):
    f = quote(json.dumps({"event": ["die", "start"]}))
    assert h.get(f"/events?filters={f}")[0] == 200
    (head,) = h.docker.heads
    sent = parse_qs(urlsplit(head.split(b" ")[1].decode()).query)
    assert json.loads(sent["filters"][0])["event"] == ["die", "start"]


def test_refusal_body_is_fixed_json(h):
    """A refusal names a short code, never echoes the target back."""
    status, resp = h.get(f"/containers/{CID}/archive?path=/etc/passwd")
    assert status == 403
    doc = json.loads(body_of(resp))
    assert set(doc) == {"message"} and "passwd" not in doc["message"]


@pytest.mark.parametrize(
    "target",
    [
        f"/containers/{CID}%2Fexport",
        f"/containers/{CID}/logs/../archive",
        "//containers/json",
        "/containers/json/",
        f"/containers/{CID}/logs;x=1",
        "/containers\\json",
        "/containers/json#frag",
        f"http://docker/containers/{CID}/export",
        "*",
    ],
)
def test_ambiguous_paths_are_named_as_such(h, target):
    """Refused for their shape before any list lookup, as the gateway does."""
    status, resp = h.get(target)
    assert status == 400 and b"ambiguous_path" in resp
