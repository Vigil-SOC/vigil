"""Follow streams (logs, events) are piped, never buffered whole, and bounded.

D4 §1: Medic follows `logs?follow=1` and `/events` through this proxy. SP2 §3:
piped byte for byte with back-pressure; a slow reader must not grow memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time

from services.medic_dockerproxy import server
from services.medic_dockerproxy.tests.conftest import (
    CID,
    STREAM_HEAD,
    chunk,
    frame,
    http_json,
)

FOLLOW = f"/containers/{CID}/logs?follow=1&stdout=1&stderr=1&timestamps=1"


def _request(s: socket.socket, target: str) -> None:
    s.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())


def _read_until(s: socket.socket, n: int, deadline: float = 5) -> bytes:
    buf, end = b"", time.monotonic() + deadline
    while len(buf) < n and time.monotonic() < end:
        data = s.recv(65536)
        if not data:
            break
        buf += data
    return buf


def test_frames_pass_through_intact(h):
    """Odd write boundaries, a 40,000-byte line (> 16 KiB split), binary bytes."""
    long_line = b"x" * 40000
    frames = [
        frame(1, b"hello\n"),
        frame(2, b'{"level":"ERROR","msg":"boom"}\n'),
        frame(1, long_line[:16384]),
        frame(1, long_line[16384:32768]),
        frame(1, long_line[32768:] + b"\n"),
        frame(2, bytes(range(256))),
    ]
    wire = STREAM_HEAD + b"".join(chunk(f) for f in frames) + b"0\r\n\r\n"

    async def handler(r, w, target):
        for i in range(0, len(wire), 997):  # boundaries that split headers and frames
            w.write(wire[i : i + 997])
            await w.drain()
            await asyncio.sleep(0)

    h.docker.routes["/logs"] = handler
    status, resp = h.get(FOLLOW)
    assert status == 200
    assert resp == wire  # Docker's reply, head and all, byte for byte


def test_quiet_stream_is_not_held_back(h):
    """One line, then silence: Medic sees the line at once (SP2 §1 finding 2)."""
    release = asyncio.Event()

    async def handler(r, w, target):
        w.write(STREAM_HEAD + chunk(frame(2, b"one line\n")))
        await w.drain()
        await release.wait()

    h.docker.routes["/logs"] = handler
    s = h.connect()
    _request(s, FOLLOW)
    t0 = time.monotonic()
    got = b""
    while b"one line\n" not in got and time.monotonic() - t0 < 2:
        got += s.recv(65536)
    assert b"one line\n" in got and time.monotonic() - t0 < 1
    h.loop.call_soon_threadsafe(release.set)
    s.close()


def test_slow_reader_does_not_grow_memory(h):
    """Docker writes as fast as it can; Medic stops reading. The proxy must stop
    reading from Docker too (back-pressure), not queue the stream in memory."""
    payload = frame(1, b"y" * 65000)

    async def handler(r, w, target):
        await h.docker.follow_until_closed(w, payload)

    h.docker.routes["/logs"] = handler
    s = h.connect(rcvbuf=4096)
    _request(s, FOLLOW)
    s.recv(1024)
    time.sleep(1.0)
    stalled_at = h.docker.sent
    time.sleep(1.0)
    grew = h.docker.sent - stalled_at
    # Kernel socket buffers on both legs plus the proxy's own bounded buffers;
    # what matters is that it stops, not the exact figure.
    assert grew <= 2 * len(payload), grew
    assert stalled_at < 16 << 20, stalled_at
    assert h.proxy.buffered() <= h.proxy.max_buffer_per_connection
    # And the stream resumes when the reader does.
    got = _read_until(s, 2 << 20)
    assert len(got) >= 2 << 20
    s.close()


def test_client_hangup_closes_the_docker_stream(h):
    """When Medic goes away, dockerd stops following (no leaked follow streams),
    even on a quiet stream where no write would ever fail."""

    async def handler(r, w, target):
        w.write(STREAM_HEAD + chunk(frame(1, b"one line\n")))
        await w.drain()
        await r.read()  # EOF: the proxy closed its side
        h.docker.closed.set()

    h.docker.routes["/logs"] = handler
    s = h.connect()
    _request(s, FOLLOW)
    s.recv(1024)
    s.close()
    h.call(asyncio.wait_for(h.docker.closed.wait(), 2))
    deadline = time.monotonic() + 3
    while h.proxy.open and time.monotonic() < deadline:
        time.sleep(0.05)
    assert h.proxy.open == 0


def test_events_stream_is_piped(h):
    async def handler(r, w, target):
        w.write(STREAM_HEAD + chunk(b'{"Type":"container","Action":"die"}\n'))
        await w.drain()
        await asyncio.sleep(0.2)
        w.write(chunk(b'{"Type":"container","Action":"start"}\n') + b"0\r\n\r\n")
        await w.drain()

    h.docker.routes["/events"] = handler
    status, resp = h.get("/events?since=1")
    assert status == 200 and b'"die"' in resp and b'"start"' in resp


def test_connection_cap(h):
    h.start_proxy(max_connections=2)

    async def handler(r, w, target):
        await h.docker.follow_until_closed(w, frame(1, b"tick\n"))

    h.docker.routes["/logs"] = handler
    held = [h.connect(), h.connect()]
    for s in held:
        _request(s, FOLLOW)
        s.recv(1024)
    status, resp = h.get("/_ping")
    assert status == 503 and b"medic-dockerproxy" in resp
    held.pop().close()
    deadline = time.monotonic() + 3
    while h.proxy.open > 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert h.get("/_ping")[0] == 200
    held[0].close()


def test_head_must_arrive_in_time(h):
    """A client trickling its request can't hold a connection slot for long."""
    h.start_proxy(head_timeout=0.5)
    s = h.connect()
    s.sendall(b"GET /_ping HTTP/1.1\r\n")
    t0 = time.monotonic()
    got = _read_until(s, 1, deadline=3)
    assert time.monotonic() - t0 < 2
    assert got == b"" or b" 400 " in got.split(b"\r\n")[0] + b" "
    s.close()


def test_docker_unreachable_is_502(h):
    h.call(_close_docker(h))
    status, resp = h.get("/containers/json")
    assert status == 502 and b"medic-dockerproxy" in resp


async def _close_docker(h) -> None:
    h._uds.close()
    await h._uds.wait_closed()
    os.unlink(h.docker.path)


def test_over_cap_flood_is_shed_at_once(h):
    """Beyond twice the cap a connection is closed without being read, so a
    flood can't pile up tasks and descriptors behind the busy reply."""
    h.start_proxy(max_connections=1)

    async def handler(r, w, target):
        await h.docker.follow_until_closed(w, frame(1, b"tick\n"))

    h.docker.routes["/logs"] = handler
    held = h.connect()
    _request(held, FOLLOW)
    held.recv(1024)
    idle = [h.connect() for _ in range(3)]  # never send a head
    # The first one waits for its head (to answer 503); the ones beyond 2 x cap
    # are closed at once, with no reply and no read.
    t0 = time.monotonic()
    shed = 0
    for s in idle:
        s.settimeout(0.5)
        try:
            shed += s.recv(1024) == b""
        except TimeoutError:
            pass
    assert shed >= 1 and time.monotonic() - t0 < 1.6
    for s in idle + [held]:
        s.close()


def test_json_reply_to_a_stalled_reader_times_out(h, monkeypatch):
    """A client that stops reading a projected reply loses its slot. The body cap
    is raised here so the reply (~17 MiB) outgrows every kernel socket buffer."""
    monkeypatch.setattr(server, "MAX_JSON_BODY", 64 << 20)
    h.start_proxy(max_connections=1, write_timeout=0.5)
    row = {"Id": "a" * 64, "Names": ["/x"], "State": "running", "Created": 1}
    rows = [row] * 150_000  # already projected: ~16 MiB in and out

    async def handler(r, w, target):
        w.write(http_json(200, json.dumps(rows).encode()))
        await w.drain()

    h.docker.routes["/containers/json"] = handler
    s = h.connect(rcvbuf=4096)
    _request(s, "/containers/json")
    assert s.recv(64).startswith(b"HTTP/1.1 200")  # the reply has started
    assert h.proxy.open == 1
    deadline = time.monotonic() + 5
    while h.proxy.open and time.monotonic() < deadline:
        time.sleep(0.05)
    assert h.proxy.open == 0
    s.close()


def test_half_close_after_the_request_ends_a_follow(h):
    """Documented, fail-safe: EOF from Medic means 'gone' (httpx doesn't half-close)."""

    async def handler(r, w, target):
        w.write(STREAM_HEAD + chunk(frame(1, b"one line\n")))
        await w.drain()
        await r.read()
        h.docker.closed.set()

    h.docker.routes["/logs"] = handler
    s = h.connect()
    _request(s, FOLLOW)
    s.shutdown(socket.SHUT_WR)
    h.call(asyncio.wait_for(h.docker.closed.wait(), 2))
    s.close()
