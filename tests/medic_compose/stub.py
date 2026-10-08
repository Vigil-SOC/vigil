"""Stand-in for a Vigil service in the live Medic Compose test. Not for installs.

One file for every role (STUB_ROLE): it listens on the role's real ports, so
reachability checks hit a real listener, and records every request so the test
can prove what the gateway let through. stdlib only; runs in python:3.12-slim.

- backend (6987): Viewer login and refresh as Vigil answers them (a JWT-shaped
  token with exp/iat); any other GET with the issued token gets `{}`.
  `GET /__seen` (from inside this container only) returns the request log.
- agent-worker (6990) / agent-serve (6989): `/readyz` 200 "ready", or 503
  "not ready" while /tmp/not-ready exists. The 503 carries STUB_CANARY in its
  body and a Set-Cookie header: Medic must keep it out of its store and logs.
- anything else: 200 `{}` on HTTP ports, accept-and-close on raw TCP ones.
"""

from __future__ import annotations

import base64
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROLE = os.environ["STUB_ROLE"]
CANARY = os.environ.get("STUB_CANARY", "")
PORTS = {
    "backend": [6987],
    "agent-worker": [6990],
    "agent-serve": [6989],
    "soc-daemon": [8081, 9090, 9091],
    "bifrost": [8080],
}
RAW_PORTS = {"redis": [6379], "postgres": [5432]}
NOT_READY = Path("/tmp/not-ready")
SEEN: list[dict] = []
LOCK = threading.Lock()


def _token() -> str:
    now = int(time.time())
    claims = base64.urlsafe_b64encode(
        json.dumps({"exp": now + 1800, "iat": now}).encode()
    )
    return "eyJhbGciOiJIUzI1NiJ9." + claims.decode().rstrip("=") + ".stub"


TOKENS: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # quiet
        pass

    def _reply(
        self, status: int, body: dict | str, headers: dict | None = None
    ) -> None:
        data = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(status)
        ctype = "application/json" if isinstance(body, dict) else "text/plain"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self) -> None:
        body = self._body()
        if ROLE == "backend":
            if self.path == "/__seen" and self.client_address[0] == "127.0.0.1":
                with LOCK:
                    return self._reply(200, {"seen": SEEN})
            with LOCK:
                SEEN.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                    }
                )
            return self._backend(body)
        if ROLE in ("agent-worker", "agent-serve") and self.path == "/readyz":
            if NOT_READY.exists():
                return self._reply(
                    503,
                    f"not ready {CANARY}",
                    {"Set-Cookie": f"session={CANARY}", "X-Debug": CANARY},
                )
            return self._reply(200, "ready")
        return self._reply(200, {})

    def _backend(self, body: bytes) -> None:
        if self.command == "POST" and self.path in (
            "/api/auth/login",
            "/api/auth/refresh",
        ):
            if self.path == "/api/auth/login":
                expected = (
                    Path("/run/secrets/medic_viewer_password").read_text().strip()
                )
                if json.loads(body or b"{}").get("password") != expected:
                    return self._reply(401, {"detail": "bad credentials"})
            access = _token()
            TOKENS.add(access)
            return self._reply(
                200, {"access_token": access, "refresh_token": "r-" + access}
            )
        auth = self.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") in TOKENS or self.path.startswith(
            "/api/health"
        ):
            return self._reply(200, {})
        return self._reply(401, {"detail": "not authenticated"})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _handle


class Raw(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.close()


def main() -> None:
    servers: list[socketserver.BaseServer] = []
    for port in PORTS.get(ROLE, []):
        servers.append(ThreadingHTTPServer(("0.0.0.0", port), Handler))
    for port in RAW_PORTS.get(ROLE, []):
        servers.append(socketserver.ThreadingTCPServer(("0.0.0.0", port), Raw))
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
