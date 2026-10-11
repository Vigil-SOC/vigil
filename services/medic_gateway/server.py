"""The two listeners: outbound (Medic -> backend) and inbound (backend -> Medic API).

stdlib only (SP1 ⚑5): `http.server` gives the exact request line, `http.client`
sends a target verbatim (httpx collapses `..` even for a raw path). Upstream
headers are built from scratch, redirects are never followed, and responses are
capped in size and total time.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from services.medic_gateway import logs, policy
from services.medic_gateway.policy import Reject
from services.medic_gateway.session import AuthUnavailable, ViewerSession

UA = "vigil-medic-gateway/1"  # fixed: Vigil binds access tokens to the User-Agent
UPSTREAM_TIMEOUT = 10.0  # total, per request, connect to last byte
CLIENT_TIMEOUT = 10.0
MAX_INFLIGHT = 8  # upstream calls in flight, per listener
MAX_CONNECTIONS = 32  # open client connections (threads), per listener
REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")  # X2's RequestId
MEDIC_KEY = re.compile(r"[A-Za-z0-9_-]{43}")  # 32 random bytes, base64url (X2)
MEDIC_ADMIN = re.compile(r"user:[A-Za-z0-9_.:/@+-]{1,123}")  # G2 id, <= 128
JSON_TYPES = ("application/json", "application/problem+json")
PASS_BACK = {
    "outbound": ("content-type", "retry-after"),
    "inbound": (
        "content-type",
        "retry-after",
        "x-request-id",
        "x-medic-api-version",
        "x-medic-sha256",
        "content-disposition",
    ),
}


class Upstream:
    def __init__(
        self, host, port, prefix="", max_body=2 << 20, timeout=UPSTREAM_TIMEOUT
    ):
        self.host, self.port, self.prefix = host, port, prefix
        self.max_body, self.timeout = max_body, timeout

    def request(self, method: str, target: str, headers: dict, body: bytes = b""):
        deadline = time.monotonic() + self.timeout
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        # The total deadline: a socket timeout alone resets on every byte.
        timer = threading.Timer(self.timeout, lambda: conn.sock and _cut(conn.sock))
        timer.start()
        try:
            conn.connect()
            conn.putrequest(
                method, self.prefix + target, skip_host=True, skip_accept_encoding=True
            )
            fixed = {
                "Host": f"{self.host}:{self.port}",
                "User-Agent": UA,
                "Accept": "application/json",
                "Connection": "close",
            }
            for k, v in {**fixed, **headers}.items():
                conn.putheader(k, v)
            if body or method == "POST":
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            resp = conn.getresponse()
            if (resp.length or 0) > self.max_body:
                raise Reject(502, "upstream_too_large")
            data = b""
            while chunk := resp.read1(65536):
                data += chunk
                if len(data) > self.max_body:
                    raise Reject(502, "upstream_too_large")
            if time.monotonic() >= deadline:
                raise TimeoutError
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        except (OSError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise TimeoutError from None
            raise
        finally:
            timer.cancel()
            conn.close()


def _cut(sock) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "medic-gateway", ""
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_TIMEOUT
    routes: tuple[policy.Route, ...] = ()
    direction = "outbound"
    upstream: Upstream
    session: ViewerSession | None = None
    slots: threading.BoundedSemaphore
    raw_method = "-"

    def log_message(self, *args):  # the stdlib access log would print raw targets
        pass

    def setup(self) -> None:
        super().setup()
        # An absolute deadline for reading the request (review #3): one byte every
        # few seconds never trips the per-read `timeout`. Cancelled once it is read.
        self.reading = threading.Timer(self.timeout, _cut, (self.connection,))
        self.reading.start()

    def finish(self) -> None:
        self.reading.cancel()
        super().finish()

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {"content-type": "application/problem+json"}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _problem(self, status: int, code: str) -> None:
        self._send(status, json.dumps({"status": status, "code": code}).encode())

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Reject as r:
            self._problem(r.status, r.code)
            self._log(r.status, r.code, None, time.monotonic())

    def parse_request(self) -> bool:
        # The raw line, parsed here: stdlib rewrites a leading '//' (gh-87389).
        parts = self.raw_requestline.rstrip(b"\r\n").decode("latin-1").split(" ")
        if len(parts) != 3 or parts[2] != "HTTP/1.1":
            self.request_version, self.close_connection = "HTTP/1.1", True
            self.command, self.requestline = "GET", ""
            raise Reject(400, "bad_request_line")
        if not super().parse_request():
            return False
        self.raw_method, self.raw_target = parts[0], parts[1]
        return True

    def _log(self, status: int, code: str, route, t0: float) -> None:
        logs.event(
            "request",
            dir=self.direction,
            method=self.raw_method[:8] if self.raw_method.isalpha() else "-",
            route=route.path if route else None,
            status=status,
            code=code,
            ms=int((time.monotonic() - t0) * 1000),
        )

    def do_any(self) -> None:
        t0, self.route = time.monotonic(), None
        if not self.slots.acquire(blocking=False):
            self._problem(503, "busy")
            return self._log(503, "busy", None, t0)
        try:
            status, code = self._dispatch()
        except Reject as r:
            status, code = r.status, r.code
        except TimeoutError:
            status, code = 504, "upstream_timeout"
        except (OSError, http.client.HTTPException):
            status, code = 502, "upstream_unreachable"
        except Exception as e:  # noqa: BLE001 - no traceback: it may hold a token
            status, code = 500, "internal_" + type(e).__name__
        finally:
            self.slots.release()
        self._log(status, code, self.route, t0)
        if code != "relayed":
            self._problem(status, code)

    def _dispatch(self):
        status_read = (self.raw_method, self.raw_target) == ("GET", "/_gw/status")
        if status_read and self.direction == "outbound" and self.session:
            body = json.dumps(self.session.status()).encode()
            self._send(200, body, {"content-type": "application/json"})
            return 200, "relayed"
        route, target = policy.check_target(
            self.raw_method, self.raw_target, self.routes
        )
        self.route = route
        policy.check_headers(self.raw_method, self.headers)
        fwd = self._forward_headers()
        body = b""
        if self.raw_method == "POST":
            body = self.rfile.read(int(self.headers["Content-Length"]))
        self.reading.cancel()
        if self.direction == "outbound" and route.auth:
            status, headers, data = self._authed(route.method, target, fwd)
        else:
            status, headers, data = self.upstream.request(
                route.method, target, fwd, body
            )
        return self._relay(status, headers, data)

    def _forward_headers(self) -> dict:
        """An allowlist: caller Authorization, Cookie, X-Forwarded-*, Forwarded,
        hop-by-hop and User-Agent are never sent on."""
        out = {}
        rid = self.headers.get("X-Request-Id")
        if rid and REQUEST_ID.fullmatch(rid):
            out["X-Request-Id"] = rid
        if self.direction == "inbound":
            key, admin = (
                self.headers.get("X-Medic-Key"),
                self.headers.get("X-Medic-Admin"),
            )
            if not key or not MEDIC_KEY.fullmatch(key):
                raise Reject(401, "unauthorized")
            out["X-Medic-Key"] = key
            if admin is not None:
                if not MEDIC_ADMIN.fullmatch(admin):
                    raise Reject(400, "bad_admin_header")
                out["X-Medic-Admin"] = admin
            if self.raw_method == "POST":
                out["Content-Type"] = "application/json"
        return out

    def _authed(self, method: str, target: str, fwd: dict):
        try:
            for attempt in (1, 2):
                tok = self.session.token()
                hdrs = {**fwd, "Authorization": f"Bearer {tok}"}
                status, headers, data = self.upstream.request(method, target, hdrs)
                if status == 401 and attempt == 2:
                    self.session.gave_up(tok)
                if status != 401 or attempt == 2 or not self.session.rejected(tok):
                    return status, headers, data
        except AuthUnavailable as e:
            raise Reject(502, f"auth_{e}") from None

    def _relay(self, status: int, headers: dict, data: bytes):
        if 300 <= status < 400:
            raise Reject(502, "upstream_redirect")  # never followed, never passed on
        if status == 401 and self.direction == "outbound":
            raise Reject(502, "auth_rejected")
        ctype = headers.get("content-type", "").split(";")[0].strip()
        if data and ctype not in JSON_TYPES:
            raise Reject(502, "upstream_content_type")
        keep = {k: headers[k] for k in PASS_BACK[self.direction] if k in headers}
        self._send(status, data, keep)
        return status, "relayed"

    def __getattr__(self, name):  # any other method token, e.g. lowercase "get"
        if name.startswith("do_"):
            return self.do_any
        raise AttributeError(name)


def make_server(
    direction, bind, upstream, session=None, routes=None
) -> ThreadingHTTPServer:
    routes = tuple(
        routes or (policy.OUTBOUND if direction == "outbound" else policy.INBOUND)
    )
    policy.check_routes(routes)
    attrs = {
        "routes": routes,
        "direction": direction,
        "upstream": upstream,
        "session": session,
        "slots": threading.BoundedSemaphore(MAX_INFLIGHT),
    }
    return _Server(bind, type(f"{direction.title()}Handler", (Handler,), attrs))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args) -> None:
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

    def handle_error(self, request, client_address) -> None:  # no traceback on stderr
        logs.event("connection_error")
