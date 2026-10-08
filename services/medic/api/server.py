"""Medic's API listener (X2): `GET /v1/status` only; the other 8 operations are G3's.

stdlib only, like the gateway (SP1 ⚑5). Every request needs `X-Medic-Key`,
compared in constant time with the key file, re-read on each request so a new
key takes over without a restart. No key, no routing: an unknown path without
the key is 401, never 404 (X2). Errors are RFC 9457 problem JSON with X2's
closed codes and no message text. Nothing about a request is logged.

The listener runs in its own threads and only reads the snapshot the main loop
publishes (`StatusBoard`), so a slow or hostile caller can't stall the loop,
and a listener that can't start never stops Medic (C5): `Api.ensure()` retries
it once a minute and `check` never looks at it.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import re
import socket
import threading
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from services.medic.api.status import API_VERSION, serve_view
from services.medic.app.config import API_KEY_FILE_VAR
from services.medic.app.wiring import TICK_S

log = logging.getLogger("services.medic")

STATUS_PATH = "/v1/status"
# The loop publishes every 15 s tick. Two missed ticks and the data is stale:
# answer `busy` rather than serve it (the watchdog ends a hung loop at 180 s).
SNAPSHOT_MAX_AGE_S = 2 * TICK_S
RETRY_AFTER_S = TICK_S
KEY_RE = re.compile(rb"[A-Za-z0-9_-]{43}")  # 32 random bytes, base64url (X2)
REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
CLIENT_TIMEOUT_S = 10.0
MAX_CONNECTIONS = 16  # one caller (the backend or the gateway), ≤ 20 req/min (C7)
RETRY_FIRST_S = 5.0  # then doubling: the gateway may still be starting
RETRY_MAX_S = 60.0


class StatusBoard:
    """The latest snapshot, handed from the main loop to the listener's threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: tuple[dict[str, Any], float, float] | None = None

    def publish(
        self, snapshot: dict[str, Any], *, started_at: float, taken_at: float
    ) -> None:
        with self._lock:
            self._current = (snapshot, started_at, taken_at)

    def read(self) -> tuple[dict[str, Any], float, float] | None:
        with self._lock:
            return self._current


def read_key(path: Path) -> bytes | None:
    """The key, or None if the file is missing, unreadable or not X2's shape."""
    try:
        raw = path.read_bytes().strip()
    except OSError:
        return None
    return raw if KEY_RE.fullmatch(raw) else None


def local_address_toward(peer: str) -> str:
    """The local IPv4 address the kernel would use to reach `peer`. A UDP
    connect sends nothing; it only picks the route."""
    ip = socket.getaddrinfo(peer, None, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ip, 9))
        return s.getsockname()[0]


def _cut(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "medic", ""
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_TIMEOUT_S
    key_file: Path
    board: StatusBoard
    wall: Callable[[], float]

    def log_message(self, *args: Any) -> None:  # the access log would print targets
        pass

    def setup(self) -> None:
        super().setup()
        # An absolute deadline for the whole request: `timeout` is per read, so a
        # byte every few seconds would never trip it (as in the gateway).
        self._deadline = threading.Timer(self.timeout, _cut, (self.connection,))
        self._deadline.start()

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _request_id(self) -> str:
        given = getattr(self, "headers", None) and self.headers.get("X-Request-Id")
        return given if given and REQUEST_ID.fullmatch(given) else str(uuid.uuid4())

    def _send(self, status: int, doc: dict[str, Any], ctype: str) -> None:
        body = json.dumps(doc, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-Id", self.rid)
        self.send_header("X-Medic-Api-Version", API_VERSION)
        if "retry_after_s" in doc:
            self.send_header("Retry-After", str(doc["retry_after_s"]))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _problem(self, status: int, code: str, **extra: Any) -> None:
        doc = {
            "type": f"urn:medic:error:{code}",
            "status": status,
            "code": code,
            "request_id": self.rid,
            **extra,
        }
        self._send(status, doc, "application/problem+json")

    def send_error(self, code: int, message: str | None = None, explain=None) -> None:
        # stdlib's own error page echoes parts of the request line; this never does.
        # Its version may still be the HTTP/0.9 default, which sends no headers.
        self.rid = str(uuid.uuid4())
        self.request_version, self.command = "HTTP/1.1", self.command or "GET"
        self._problem(400, "invalid_parameter")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # Routed on the raw target: stdlib rewrites a leading '//' (gh-87389).
        self.raw_target = self.raw_requestline.split(b" ")[1].decode("latin-1")
        return True

    def _authorised(self) -> bool:
        given = self.headers.get("X-Medic-Key")
        key = read_key(self.key_file)
        if given is None or key is None:
            return False
        return hmac.compare_digest(given.encode("latin-1", "replace"), key)

    def do_any(self) -> None:
        self._deadline.cancel()
        self.rid = self._request_id()
        if not self._authorised():
            return self._problem(401, "unauthorized")
        if self.raw_target != STATUS_PATH:
            return self._problem(404, "not_found")
        if self.command != "GET":
            return self._problem(405, "method_not_allowed")
        current = self.board.read()
        now = self.wall()
        if current is None or now - current[2] > SNAPSHOT_MAX_AGE_S:
            return self._problem(503, "busy", retry_after_s=RETRY_AFTER_S)
        snapshot, started_at, _ = current
        doc = serve_view(snapshot, now=now, started_at=started_at)
        self._send(200, doc, "application/json")

    def __getattr__(self, name: str):  # every method token, e.g. "POST" or "get"
        if name.startswith("do_"):
            return self.do_any
        raise AttributeError(name)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.conn_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address) -> None:
        if not self.conn_slots.acquire(blocking=False):  # over the cap: close at once
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.conn_slots.release()

    def handle_error(self, request, client_address) -> None:  # no traceback, no peer
        log.debug("Medic API: a connection ended with an error")


def make_server(
    bind: tuple[str, int],
    *,
    key_file: Path,
    board: StatusBoard,
    wall: Callable[[], float],
) -> ThreadingHTTPServer:
    attrs = {"key_file": key_file, "board": board, "wall": staticmethod(wall)}
    return _Server(bind, type("MedicApiHandler", (Handler,), attrs))


class Api:
    """Starts the listener when it can, and keeps trying when it can't: after 5 s,
    doubling to once a minute. Called from the main loop each cycle. Each distinct
    problem is logged once; none of them is a reason for Medic to stop (C5).

    With `bind_peer` (Compose: the gateway's medic-private alias), the listener
    binds only the local address on the network that reaches that peer, not every
    interface: Medic is also on medic-net, with the agents (K1 §6 G3)."""

    def __init__(
        self,
        bind: tuple[str, int],
        *,
        key_file: Path | None,
        board: StatusBoard,
        wall: Callable[[], float],
        monotonic: Callable[[], float],
        bind_peer: str | None = None,
        retry_s: float = RETRY_MAX_S,
    ) -> None:
        self.bind, self.key_file, self.board = bind, key_file, board
        self.bind_peer = bind_peer
        self._wall, self._monotonic, self._retry_s = wall, monotonic, retry_s
        self._next_try: float | None = None
        self._failures = 0
        self._said: str | None = None
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.server is not None

    def _say_once(self, what: str, message: str, *args: Any) -> None:
        if self._said != what:
            self._said = what
            log.warning(message, *args)

    def ensure(self) -> None:
        if self.server is not None:
            return
        now = self._monotonic()
        if self._next_try is not None and now < self._next_try:
            return
        if not self._try_start():
            self._failures += 1
            delay = RETRY_FIRST_S * 2 ** min(self._failures - 1, 8)
            self._next_try = now + min(self._retry_s, delay)

    def _try_start(self) -> bool:
        if self.key_file is None:
            self._say_once(
                "no-setting",
                "Medic's API is off: %s isn't set, so no caller could be checked. "
                "Medic keeps watching; the console will read Down.",
                API_KEY_FILE_VAR,
            )
            return False
        if read_key(self.key_file) is None:
            self._say_once(
                "no-key",
                "Medic's API is off: no usable key at %s (32 random bytes, "
                "base64url, 43 characters). Medic keeps watching; retrying every "
                "%.0f s.",
                self.key_file,
                self._retry_s,
            )
            return False
        host, port = self.bind
        if self.bind_peer is not None:
            try:
                host = local_address_toward(self.bind_peer)
            except OSError as exc:
                self._say_once(
                    "peer",
                    "Medic's API isn't listening yet: it listens only on the "
                    "network that reaches %s, which doesn't resolve (%s; is the "
                    "gateway up?). Medic keeps watching; retrying.",
                    self.bind_peer,
                    type(exc).__name__,
                )
                return False
        try:
            server = make_server(
                (host, port), key_file=self.key_file, board=self.board, wall=self._wall
            )
        except OSError as exc:
            self._say_once(
                "bind",
                "Medic's API can't listen on %s:%d (%s). Medic keeps watching; "
                "retrying every %.0f s at most.",
                host,
                port,
                type(exc).__name__,
                self._retry_s,
            )
            return False
        self.server, self._said, self._failures = server, None, 0
        self._thread = threading.Thread(
            target=server.serve_forever, name="medic-api", daemon=True
        )
        self._thread.start()
        log.info("Medic's API is listening on %s:%d", *server.server_address[:2])
        return True

    def stop(self) -> None:
        if self.server is None:
            return
        server, self.server = self.server, None
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
