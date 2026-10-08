"""A stub Vigil backend (or Medic API) and a gateway wired to it on loopback."""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.medic_gateway import server
from services.medic_gateway.session import ViewerSession

PASSWORD = "canary-PW-" + "x" * 30


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def make_token(kind: str, ttl: int, n: int) -> str:
    claims = {"exp": int(time.time()) + ttl, "token_type": kind, "n": n}
    return f"{_b64({'alg': 'HS256'})}.{_b64(claims)}.sig{n}"


class Stub:
    """Fake upstream. Records every request; behaviour is set per test."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        self.login_status = 200
        self.refresh_status = 200
        self.access_ttl = 1800
        self.reject_tokens: set[str] = set()
        self.reject_all = False
        self.issued = 0
        self.status = 200
        self.body = b'{"ok": true}'
        self.content_type = "application/json"
        self.extra: dict[str, str] = {"X-Medic-Api-Version": "1.0"}
        self.delay = 0.0
        self.drip = 0.0
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, status, data: bytes, ctype="application/json", extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Set-Cookie", "access_token=leak; Path=/")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if stub.drip:
                    for b in data:
                        time.sleep(stub.drip)
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                else:
                    self.wfile.write(data)

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                stub.seen.append(
                    {
                        "method": self.command,
                        "target": self.path,
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                if self.path.endswith("/api/auth/login"):
                    if stub.login_status != 200:
                        extra = {"Retry-After": "77"}
                        return self._reply(stub.login_status, b"{}", extra=extra)
                    return self._issue()
                if self.path.endswith("/api/auth/refresh"):
                    if stub.refresh_status != 200:
                        return self._reply(stub.refresh_status, b"{}")
                    return self._issue()
                time.sleep(stub.delay)
                auth = self.headers.get("Authorization", "")
                if auth and (stub.reject_all or auth.split()[-1] in stub.reject_tokens):
                    return self._reply(401, b'{"detail": "Invalid or expired token"}')
                extra = dict(stub.extra)
                if stub.status in (301, 302, 307, 308):
                    extra["Location"] = "/api/health"
                self._reply(stub.status, stub.body, stub.content_type, extra)

            def _issue(self):
                stub.issued += 1
                n, ttl = stub.issued, stub.access_ttl
                body = {
                    "access_token": make_token("access", ttl, n),
                    "refresh_token": make_token("refresh", 604800, n),
                    "token_type": "bearer",
                }
                self._reply(200, json.dumps(body).encode())

            do_GET = do_POST = _handle

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(
            target=self.srv.serve_forever, args=(0.02,), daemon=True
        ).start()
        self.port = self.srv.server_address[1]

    def data(self) -> list[dict]:
        return [s for s in self.seen if "/api/auth/" not in s["target"]]

    def targets(self) -> list[str]:
        return [s["target"] for s in self.seen]


class Clock:
    """Real time plus an offset the test moves forward."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


@dataclass
class Env:
    backend: Stub
    medic: Stub
    session: ViewerSession
    clock: Clock
    pw: Path
    out: int
    inn: int
    out_srv: object
    inn_srv: object


@pytest.fixture
def env(tmp_path: Path):
    pw = tmp_path / "viewer.pw"
    pw.write_text(PASSWORD + "\n")
    backend, medic, clock = Stub(), Stub(), Clock()
    up = server.Upstream("127.0.0.1", backend.port)
    session = ViewerSession(up, "medic-viewer", pw, clock=clock)
    out = server.make_server("outbound", ("127.0.0.1", 0), up, session)
    medic_up = server.Upstream("127.0.0.1", medic.port, max_body=8 << 20)
    inn = server.make_server("inbound", ("127.0.0.1", 0), medic_up)
    for s in (out, inn):
        threading.Thread(target=s.serve_forever, args=(0.02,), daemon=True).start()
    yield Env(
        backend,
        medic,
        session,
        clock,
        pw,
        out.server_address[1],
        inn.server_address[1],
        out,
        inn,
    )
    for s in (out, inn, backend.srv, medic.srv):
        s.shutdown()
        s.server_close()


def send(port: int, raw: bytes, timeout: float = 5) -> tuple[int, dict, bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(raw)
    buf = b""
    try:
        while chunk := s.recv(65536):
            buf += chunk
    except (TimeoutError, ConnectionResetError):
        pass
    s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return int(lines[0].split()[1]), hdrs, body


def get(env: Env, path: bytes = b"/api/federation/sources") -> int:
    raw = b"GET " + path + b" HTTP/1.1\r\nHost: g\r\nConnection: close\r\n\r\n"
    return send(env.out, raw)[0]


def logins(env: Env) -> list[dict]:
    return [s for s in env.backend.seen if s["target"].endswith("/api/auth/login")]


KEY = "k" * 43


def inbound(env: Env, method: bytes, target: bytes, headers=b"", body=b""):
    raw = method + b" " + target + b" HTTP/1.1\r\nHost: g\r\n" + headers
    raw += b"Connection: close\r\n"
    if body:
        raw += b"Content-Type: application/json\r\nContent-Length: "
        raw += str(len(body)).encode() + b"\r\n"
    return send(env.inn, raw + b"\r\n" + body)


def keyed(extra: bytes = b"") -> bytes:
    return b"X-Medic-Key: " + KEY.encode() + b"\r\n" + extra
