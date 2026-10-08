"""A fake Docker Engine on a Unix socket, and the proxy wired to it on loopback.

Both run on one event loop in a background thread; tests talk to the proxy with
plain blocking sockets, the way Medic's client would.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from services.medic_dockerproxy import server

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = b"s5p-hunter2-secret"  # planted in the fixtures' Env, Cmd, Args, Health.Log
CID = "medic-s5p-fixture"

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, str], Awaitable[None]]


def http_json(status: int, body: bytes, chunked: bool = False) -> bytes:
    head = f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
    if chunked:
        mid = len(body) // 2
        parts = [body[:mid], body[mid:]]
        payload = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts if p)
        return (
            (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
            + payload
            + (b"0\r\n\r\n")
        )
    return (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body


def frame(stream: int, payload: bytes) -> bytes:
    """One frame of Docker's multiplexed log stream (SP2 §2 row 1a)."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


STREAM_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.docker.multiplexed-stream\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n"
)


class FakeDocker:
    """Records each request's raw head; replies per path, overridable per test."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="s5p", dir="/tmp")  # AF_UNIX ≤ 104 bytes
        self.path = f"{self.dir}/docker.sock"
        self.heads: list[bytes] = []
        self.closed = asyncio.Event()  # set when a client hangs up on a stream
        self.sent = 0  # bytes a streaming handler got past drain()
        self.inspect = (FIXTURES / "inspect.json").read_bytes()
        self.listing = (FIXTURES / "list.json").read_bytes()
        self.routes: dict[str, Handler] = {}

    async def _default(self, r, w, target: str) -> None:
        path = target.split("?")[0]
        if path.endswith("/_ping"):
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        elif path.endswith("/containers/json"):
            w.write(http_json(200, self.listing, chunked=True))
        elif path.endswith("/json"):
            w.write(http_json(200, self.inspect))
        elif path.endswith("/events"):
            ev = json.dumps({"Type": "container", "Action": "start"}).encode()
            w.write(STREAM_HEAD + chunk(ev + b"\n") + b"0\r\n\r\n")
        elif path.endswith("/logs"):
            w.write(STREAM_HEAD + chunk(frame(2, b"started\n")) + b"0\r\n\r\n")
        else:
            w.write(http_json(404, b'{"message":"page not found"}'))
        await w.drain()

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await r.readuntil(b"\r\n\r\n")
            self.heads.append(head)
            target = head.split(b" ")[1].decode()
            path = target.split("?")[0]
            handler = next(
                (h for k, h in self.routes.items() if path.endswith(k)), self._default
            )
            await handler(r, w, target)
        except (ConnectionError, asyncio.IncompleteReadError):
            self.closed.set()
        finally:
            w.close()

    async def follow_until_closed(self, w: asyncio.StreamWriter, data: bytes) -> None:
        """Write `data` as chunks until the far side goes away; counts bytes sent."""
        w.write(STREAM_HEAD)
        try:
            while True:
                w.write(chunk(data))
                await w.drain()
                self.sent += len(data)
        except ConnectionError:
            self.closed.set()


class Harness:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.docker = FakeDocker()
        self.call(self._start_docker())
        self.proxy: server.Proxy | None = None
        self.port = 0

    def call(self, coro, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _start_docker(self) -> None:
        self.docker.closed = asyncio.Event()
        self._uds = await asyncio.start_unix_server(
            self.docker.handle, self.docker.path
        )

    async def _close_docker(self) -> None:
        self._uds.close()  # on the loop's thread: Server isn't thread-safe

    def start_proxy(self, **kw) -> None:
        if self.proxy is not None:
            self.call(self.proxy.stop())
        self.proxy = server.Proxy(self.docker.path, **kw)
        self.port = self.call(self.proxy.start("127.0.0.1", 0))

    def connect(self, rcvbuf: int | None = None) -> socket.socket:
        s = socket.socket()
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.settimeout(5)
        s.connect(("127.0.0.1", self.port))
        return s

    def raw(self, req: bytes, timeout: float = 5) -> tuple[int, bytes]:
        s = self.connect()
        s.settimeout(timeout)
        s.sendall(req)
        buf = b""
        try:
            while data := s.recv(65536):
                buf += data
        except TimeoutError:
            pass
        s.close()
        status = int(buf.split(b" ", 2)[1]) if buf else 0
        return status, buf

    def get(self, target: str, extra: str = "", method: str = "GET"):
        return self.raw(
            f"{method} {target} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode()
        )

    def close(self) -> None:
        if self.proxy is not None:
            self.call(self.proxy.stop())
        self.call(self._close_docker())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        shutil.rmtree(self.docker.dir, ignore_errors=True)


@pytest.fixture
def h():
    harness = Harness()
    harness.start_proxy()
    yield harness
    harness.close()


def body_of(resp: bytes) -> bytes:
    return resp.split(b"\r\n\r\n", 1)[1]
