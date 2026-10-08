"""Inspect and list replies are projected to SP2 §3's field allowlist (⚑1 (a)).

The fixtures are real Docker 29.4 / API 1.54 replies for a container with a
password planted in Env, in its command line (the `misp-redis --requirepass`
shape, SP2 ⚑1) and in its health probe's output.
"""

from __future__ import annotations

import json

import pytest

from services.medic_dockerproxy import project
from services.medic_dockerproxy.tests.conftest import (
    CID,
    FIXTURES,
    SECRET,
    body_of,
    http_json,
)

INSPECT_FIELDS = {
    "Id",
    "Name",
    "Created",
    "RestartCount",
    "Image",
    "State",
    "Config",
    "HostConfig",
}
LIST_FIELDS = {"Id", "Names", "Image", "Labels", "State", "Status", "Created"}


def test_negative_control_raw_docker_reply_carries_the_secret(h):
    """Without the proxy the secret is in Env, Cmd, Args and Health.Log."""
    raw = json.loads(h.docker.inspect)
    assert any(SECRET.decode() in e for e in raw["Config"]["Env"])
    assert any(SECRET.decode() in a for a in raw["Config"]["Cmd"])
    assert any(SECRET.decode() in a for a in raw["Args"])
    assert SECRET.decode() in raw["State"]["Health"]["Log"][0]["Output"]
    assert SECRET in h.docker.listing  # via Command


def test_inspect_through_proxy_has_only_allowlisted_fields(h):
    status, resp = h.get(f"/containers/{CID}/json")
    assert status == 200
    body = body_of(resp)
    assert SECRET not in resp
    doc = json.loads(body)
    assert set(doc) == INSPECT_FIELDS
    for gone in ("Args", "Path", "Mounts", "NetworkSettings", "LogPath"):
        assert gone not in doc
    assert set(doc["Config"]) == {"Image", "Labels", "Tty"}
    for gone in ("Env", "Cmd", "Entrypoint", "Healthcheck", "User", "WorkingDir"):
        assert gone not in doc["Config"]
    assert set(doc["State"]["Health"]) == {"Status", "FailingStreak"}  # no Log
    assert "Pid" not in doc["State"] and "Error" not in doc["State"]
    assert set(doc["HostConfig"]) <= {
        "LogConfig",
        "RestartPolicy",
        "Memory",
        "NanoCpus",
    }
    assert set(doc["HostConfig"]["LogConfig"]) == {"Type"}  # no Config (log-opts)
    assert doc["State"]["Running"] is True
    assert doc["Config"]["Labels"]["com.docker.compose.service"] == "misp-redis"


def test_list_through_proxy_has_only_allowlisted_fields(h):
    status, resp = h.get("/containers/json")
    assert status == 200 and SECRET not in resp and b'"Command"' not in resp
    (row,) = json.loads(body_of(resp))
    assert set(row) == LIST_FIELDS
    assert row["Names"] == [f"/{CID}"] and row["State"] == "running"


def _serve_inspect(h, raw_reply: bytes) -> tuple[int, bytes]:
    async def handler(r, w, target):
        w.write(raw_reply)
        await w.drain()

    h.docker.routes["/json"] = handler
    return h.get(f"/containers/{CID}/json")


def project_fixture() -> bytes:
    return (FIXTURES / "inspect.json").read_bytes()


def _with(**changes) -> bytes:
    """The inspect fixture with one field replaced (`A__B` = doc["A"]["B"])."""
    doc = json.loads(project_fixture())
    for dotted, value in changes.items():
        node = doc
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return json.dumps(doc).encode()


S = SECRET.decode()
MALFORMED = {
    "not json": http_json(200, b'{"Config": {"Env": ["P=' + SECRET + b'"'),
    "top-level list": http_json(200, b'[{"Env": "' + SECRET + b'"}]'),
    "top-level string": http_json(200, json.dumps(S).encode()),
    "Config is a string": http_json(200, _with(Config=f"--requirepass {S}")),
    "State is a list": http_json(200, _with(State=[S])),
    "Health is a string": http_json(200, _with(State__Health=S)),
    "Labels hold a non-string": http_json(200, _with(Config__Labels={"a": [S]})),
    "Labels is a list": http_json(200, _with(Config__Labels=[S])),
    "Name is a dict": http_json(200, _with(Name={"x": S})),
    "Running is a string": http_json(200, _with(State__Running=S)),
    "RestartCount is a bool": http_json(200, _with(RestartCount=True)),
    "RestartPolicy holds a dict": http_json(
        200, _with(HostConfig__RestartPolicy={"Name": {"x": S}})
    ),
    "body cut short": b'HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\n{"Env":"'
    + SECRET,
    "bad chunk size": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n"
    + SECRET,
    "no status line": SECRET + b"\r\n\r\n",
    "non-JSON content type is still parsed, and fails": b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain\r\nContent-Length: 18\r\n\r\n" + SECRET,
    "huge": b"HTTP/1.1 200 OK\r\nContent-Length: 999999999\r\n\r\n" + SECRET,
    "no framing": b"HTTP/1.1 200 OK\r\n\r\n" + SECRET,
}


@pytest.mark.parametrize("case", MALFORMED)
def test_malformed_inspect_fails_closed(h, case):
    status, resp = _serve_inspect(h, MALFORMED[case])
    assert status == 502, resp[:300]
    assert SECRET not in resp
    assert json.loads(body_of(resp)) == {"message": "medic-dockerproxy: upstream_reply"}


def test_nulls_and_missing_fields_pass(h):
    """A container without a health check: Health absent; Labels may be null."""
    doc = json.loads(project_fixture())
    del doc["State"]["Health"]
    doc["Config"]["Labels"] = None
    status, resp = _serve_inspect(h, http_json(200, json.dumps(doc).encode()))
    assert status == 200
    out = json.loads(body_of(resp))
    assert "Health" not in out["State"] and out["Config"]["Labels"] is None


@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            http_json(404, b'{"message":"No such container: x"}'),
            {"message": "No such container: x"},
        ),
        (
            http_json(500, b'{"message":"m","Env":["' + SECRET + b'"]}'),
            {"message": "m"},
        ),
        (http_json(500, b"not json " + SECRET), {"message": "docker error"}),
        (
            http_json(404, b'{"message": ["' + SECRET + b'"]}'),
            {"message": "docker error"},
        ),
    ],
)
def test_docker_errors_pass_only_their_message(h, reply, expected):
    status, resp = _serve_inspect(h, reply)
    assert status == int(reply.split(b" ")[1])
    assert json.loads(body_of(resp)) == expected
    assert SECRET not in resp


def test_list_with_a_bad_row_fails_closed(h):
    rows = json.loads(h.docker.listing)
    rows.append({"Id": "x", "Names": f"--requirepass {S}"})

    async def handler(r, w, target):
        w.write(http_json(200, json.dumps(rows).encode()))
        await w.drain()

    h.docker.routes["/containers/json"] = handler
    status, resp = h.get("/containers/json")
    assert status == 502 and SECRET not in resp


def test_project_is_deny_by_default():
    """A field Docker adds tomorrow is dropped, whatever its name."""
    doc = json.loads(project_fixture())
    doc["NewSecretField"] = S
    doc["State"]["NewThing"] = S
    doc["Config"]["Secrets"] = [S]
    out = json.dumps(project.inspect(doc))
    assert S not in out


def test_project_rejects_bool_for_int_and_int_for_bool():
    with pytest.raises(project.Unprojectable):
        project.project(True, int)
    with pytest.raises(project.Unprojectable):
        project.project(1, bool)


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_reply_is_refused_not_buffered(h, chunked):
    """A valid reply over 8 MiB is refused, whatever its framing."""
    doc = json.loads(project_fixture())
    doc["Padding"] = "p" * (9 << 20)
    status, resp = _serve_inspect(h, http_json(200, json.dumps(doc).encode(), chunked))
    assert status == 502 and SECRET not in resp
