"""The proxy: one validated GET per connection, rebuilt and sent to the socket.

Streaming routes (logs, events, ping) are piped byte for byte with
back-pressure: the proxy reads from Docker only as fast as Medic reads from it,
so a slow reader holds at most a few bounded buffers, never the stream. JSON
routes (list, inspect) are read whole (bounded), parsed and projected; anything
that doesn't parse fails closed with 502 (SP2 §3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from services.medic_dockerproxy import logs, policy, project
from services.medic_dockerproxy.policy import Refuse

CHUNK = 64 * 1024  # one read from Docker; also the upstream StreamReader limit
WRITE_HIGH = 64 * 1024  # the client transport's high-water mark
MAX_JSON_BODY = 8 << 20
MAX_CONNECTIONS = 32  # D4: ~7 follow streams + events + polls
HEAD_TIMEOUT = 10.0
UPSTREAM_TIMEOUT = 10.0  # connect, and a whole JSON reply
WRITE_TIMEOUT = 10.0  # sending a projected JSON reply to Medic
STATUS_RE = re.compile(r"HTTP/1\.[01] ([1-5][0-9]{2})( .*)?")
SIZE_RE = re.compile(rb"[0-9A-Fa-f]{1,8}")
LENGTH_RE = re.compile(r"[0-9]{1,10}")
REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


async def read_reply(ur: asyncio.StreamReader) -> tuple[int, bytes]:
    """Docker's status and body. Raises ValueError on anything unexpected."""
    head = (await ur.readuntil(b"\r\n\r\n"))[:-4].decode("latin-1")
    line, *lines = head.split("\r\n")
    m = STATUS_RE.fullmatch(line)
    if m is None:
        raise ValueError("status line")
    hdrs: dict[str, str] = {}
    for h in lines:
        name, colon, value = h.partition(":")
        name = name.strip().lower()
        if not colon or name in hdrs:
            raise ValueError("header")
        hdrs[name] = value.strip()
    te = hdrs.get("transfer-encoding")
    if te is not None:
        if te.lower() != "chunked":
            raise ValueError("transfer-encoding")
        body = bytearray()
        while True:
            size = (await ur.readuntil(b"\r\n"))[:-2].split(b";")[0]
            if not SIZE_RE.fullmatch(size):
                raise ValueError("chunk size")
            n = int(size, 16)
            if n == 0:
                return int(m[1]), bytes(body)
            if len(body) + n > MAX_JSON_BODY:
                raise ValueError("too large")
            body += await ur.readexactly(n)
            if await ur.readexactly(2) != b"\r\n":
                raise ValueError("chunk end")
    length = hdrs.get("content-length", "")
    if not LENGTH_RE.fullmatch(length) or int(length) > MAX_JSON_BODY:
        raise ValueError("content-length")
    return int(m[1]), await ur.readexactly(int(length))


def _message(body: bytes) -> str:
    """Docker's error text, and nothing else from an error body."""
    try:
        doc = json.loads(body)
    except ValueError:
        return "docker error"
    msg = doc.get("message") if isinstance(doc, dict) else None
    return msg[:500] if isinstance(msg, str) else "docker error"


async def send(cw: asyncio.StreamWriter, status: int, body: bytes) -> None:
    head = (
        f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    cw.write(head.encode() + body)
    await cw.drain()


async def refuse(cw: asyncio.StreamWriter, status: int, code: str) -> None:
    await send(
        cw, status, json.dumps({"message": f"medic-dockerproxy: {code}"}).encode()
    )


class Proxy:
    # Bound on what a streaming connection holds: the upstream reader pauses
    # Docker at 2 x its limit, one chunk is in hand, and the client buffer stops at
    # WRITE_HIGH. A JSON route holds its projected reply (≤ MAX_JSON_BODY) for at
    # most WRITE_TIMEOUT.
    max_buffer_per_connection = 3 * CHUNK + WRITE_HIGH

    def __init__(
        self,
        socket_path: str,
        max_connections: int = MAX_CONNECTIONS,
        head_timeout: float = HEAD_TIMEOUT,
        write_timeout: float = WRITE_TIMEOUT,
    ) -> None:
        self.socket_path = socket_path
        self.max_connections = max_connections
        self.head_timeout = head_timeout
        self.write_timeout = write_timeout
        self.open = 0
        self.waiting = 0  # over the cap, being told 503
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(
            self._handle, host, port, limit=policy.MAX_HEAD
        )
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    def buffered(self) -> int:
        """Bytes queued towards clients now, the largest per connection."""
        sizes = [w.transport.get_write_buffer_size() for w in self._clients]
        return max(sizes, default=0)

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            if self.open >= self.max_connections:
                if self.waiting >= self.max_connections:
                    logs.event("shed", level=logging.WARNING)
                    return  # beyond 2 x cap: close unread, hold nothing
                self.waiting += 1
                try:
                    await self._busy(cr, cw)
                finally:
                    self.waiting -= 1
                return
            self.open += 1
            self._clients.add(cw)
            try:
                await self._serve(cr, cw)
            finally:
                self.open -= 1
                self._clients.discard(cw)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            self._tasks.discard(task)
            cw.close()

    async def _busy(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        logs.event("busy", level=logging.WARNING)
        # Read the head first (briefly): closing on unread bytes sends a reset,
        # and the client would see no answer at all.
        try:
            await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 1)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        await refuse(cw, 503, "busy")

    async def _serve(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), self.head_timeout)
            req = policy.check_head(head[:-4])
        except Refuse as e:
            logs.event("refused", code=e.code)
            return await refuse(cw, e.status, e.code)
        except asyncio.LimitOverrunError:
            return await refuse(cw, 431, "head_too_large")
        except TimeoutError:
            return await refuse(cw, 400, "head_timeout")
        except asyncio.IncompleteReadError:
            return
        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path, limit=CHUNK),
                UPSTREAM_TIMEOUT,
            )
        except (OSError, TimeoutError):
            logs.event("upstream_unreachable", level=logging.WARNING)
            return await refuse(cw, 502, "upstream_unreachable")
        try:
            uw.write(
                f"GET {req.target} HTTP/1.1\r\nHost: docker\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            await uw.drain()
            if req.route in policy.STREAMING:
                await self._pipe(ur, cr, cw)
            else:
                await self._projected(req.route, ur, cw)
        finally:
            uw.close()

    async def _pipe(self, ur, cr, cw) -> None:
        cw.transport.set_write_buffer_limits(high=WRITE_HIGH)

        async def pump() -> None:
            while data := await ur.read(CHUNK):
                cw.write(data)
                await cw.drain()  # back-pressure: wait for Medic before reading on

        # Until Docker ends the stream or Medic hangs up (or sends anything more:
        # one request per connection). Either way the Docker side is closed, so
        # dockerd stops following. A half-close (SHUT_WR) after the request also
        # reads as "gone": fail-safe, and Medic's client (httpx) doesn't do it.
        tasks = {asyncio.ensure_future(pump()), asyncio.ensure_future(cr.read(1))}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _projected(self, route: str, ur, cw) -> None:
        try:
            status, body = await asyncio.wait_for(read_reply(ur), UPSTREAM_TIMEOUT)
            if status == 200:
                doc = json.loads(body)
                out = (
                    project.inspect(doc) if route == "inspect" else project.listing(doc)
                )
            else:
                out = {"message": _message(body)}
        except (
            ValueError,  # includes JSON and Unicode decode errors
            RecursionError,
            project.Unprojectable,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            logs.event("upstream_reply_refused", level=logging.WARNING, route=route)
            return await refuse(cw, 502, "upstream_reply")
        reply = json.dumps(out, separators=(",", ":")).encode()
        await asyncio.wait_for(send(cw, status, reply), self.write_timeout)
