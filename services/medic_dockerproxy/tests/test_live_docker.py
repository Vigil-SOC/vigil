"""SP2's 43 proxy tests, ported: C3 §7 check 4 against a live Docker Engine.

Skipped when there is no Docker socket; CI's runner has one, so the job runs
them. The negative control proves the raw Docker reply does carry the secret,
so the proxy tests aren't passing vacuously.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import quote

import pytest

from services.medic_dockerproxy import server
from services.medic_dockerproxy.tests.test_policy import REFUSED as ALL_REFUSED

pytestmark = pytest.mark.docker

_HOST = os.environ.get("DOCKER_HOST", "")  # noqa: ENV001 - finds the engine
SOCK = _HOST.removeprefix("unix://") or "/var/run/docker.sock"
SECRET = "s5p-live-hunter2-secret"
NAME = f"medic-s5p-target-{os.getpid()}"


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(SOCK)
        return True
    except OSError:
        return False


if not _docker_up():
    pytest.skip("no live Docker Engine", allow_module_level=True)


def raw_docker(path: str) -> bytes:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(SOCK)
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: d\r\nConnection: close\r\n\r\n".encode()
        )
        buf = b""
        while data := s.recv(65536):
            buf += data
    return buf


@pytest.fixture(scope="module")
def live():
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", NAME,
            "-e", f"POSTGRES_PASSWORD={SECRET}",
            "alpine:3.20", "sh", "-c", f"echo started; exec sleep 600 # --requirepass {SECRET}",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    proxy = server.Proxy(SOCK)
    port = asyncio.run_coroutine_threadsafe(proxy.start("127.0.0.1", 0), loop).result(
        10
    )
    yield port
    asyncio.run_coroutine_threadsafe(proxy.stop(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)


def get(port: int, target: str, extra: str = "", method: str = "GET"):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"{method} {target} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
    buf = b""
    try:
        while data := s.recv(65536):
            buf += data
    except TimeoutError:
        pass
    s.close()
    return (int(buf.split(b" ", 2)[1]) if buf else 0), buf


def body(resp: bytes) -> bytes:
    return resp.split(b"\r\n\r\n", 1)[1]


# ---- allowed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "t",
    [
        "/_ping",
        "/containers/json",
        "/v1.54/containers/json",
        "/containers/json?all=1&filters=%7B%7D",
    ],
)
def test_allowed_reads(live, t):
    assert get(live, t)[0] == 200


def test_list_with_encoded_label_filter(live):
    f = quote(json.dumps({"label": ["com.docker.compose.project=vigil"]}))
    assert get(live, f"/containers/json?filters={f}")[0] == 200


def test_inspect_has_no_env_cmd_or_secret(live):
    st, resp = get(live, f"/containers/{NAME}/json")
    assert st == 200
    doc = json.loads(body(resp))
    assert "Env" not in doc["Config"] and "Cmd" not in doc["Config"]
    assert "Entrypoint" not in doc["Config"] and "Args" not in doc and "Path" not in doc
    assert SECRET.encode() not in resp
    assert doc["State"]["Running"] is True and "Labels" in doc["Config"]


def test_control_docker_direct_does_leak(live):
    """Negative control: without the proxy the secret is in Env, Cmd/Args and Command."""
    for path in (f"/containers/{NAME}/json", "/containers/json"):
        assert SECRET.encode() in raw_docker(path), path


def test_list_has_no_command_or_secret(live):
    st, resp = get(live, "/containers/json")
    assert st == 200 and SECRET.encode() not in resp and b'"Command"' not in resp


def test_logs_non_follow(live):
    deadline = time.monotonic() + 5
    while True:
        st, resp = get(live, f"/containers/{NAME}/logs?stdout=1&stderr=1&timestamps=1")
        if b"started" in resp or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert st == 200 and b"started" in resp


def test_events_type_forced_to_container(live):
    now = int(time.time())
    st, resp = get(live, f"/events?since={now - 600}&until={now}")
    assert st == 200
    assert b'"Type":"image"' not in body(resp) and b'"Type":"network"' not in body(resp)


# ---- refused (C3 §7 check 4 + bypass shapes): SP2's 32 rows -----------------

REFUSED = [(m, t.replace("medic-s5p-fixture", NAME), x) for m, t, x in ALL_REFUSED[:32]]


@pytest.mark.parametrize("method,target,extra", REFUSED)
def test_refused(live, method, target, extra):
    st, resp = get(live, target, extra, method)
    assert st in (400, 403, 405), resp[:200]
    assert b"medic-dockerproxy" in resp


def test_container_still_running_after_refusals(live):
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}} {{.RestartCount}}", NAME],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert out == "true 0"
