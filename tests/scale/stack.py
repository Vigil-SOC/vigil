"""The stack the weekly scale run measures, and the token it reads and writes with.

The real backend, soc-daemon, Postgres and Redis from infra/docker/docker-compose.yml
under compose.scale.yml, in their own Compose project. Auth is on: an admin is made
through the first-run flow, sets up MFA, and creates a service account whose token
every measured request carries. Nothing here calls a model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import struct
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "infra" / "docker" / "docker-compose.yml"
OVERRIDE = Path(__file__).with_name("compose.scale.yml")
PROJECT = "vigil-scale"
IMAGES = ("backend", "daemon")  # <project>-<name>:local, see compose.scale.yml
ADMIN = "scale-admin"
SERVICE_ACCOUNT = "scale-runner"


class StackFailed(RuntimeError):
    pass


def totp(secret: str) -> str:
    """The current 6-digit code for a base32 secret (RFC 6238)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    at = digest[-1] & 15
    return "%06d" % ((struct.unpack(">I", digest[at:at + 4])[0] & 0x7FFFFFFF) % 10**6)


class Stack:
    def __init__(self) -> None:
        self.webhook_token = "scalehook" + secrets.token_hex(12)
        self.csrf = secrets.token_urlsafe(24)  # the double-submit pair
        self.token = ""  # the service account's
        self.api_port = 0
        self.intake_port = 0
        # Built from scratch, so a developer's keys and .env never reach the stack.
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")),
            "COMPOSE_PROJECT_NAME": PROJECT,
            "REDIS_PASSWORD": "scaleredis" + secrets.token_hex(12),
            "POSTGRES_PASSWORD": "scalepg" + secrets.token_hex(12),
            "POSTGRES_APP_PASSWORD": "scaleapp" + secrets.token_hex(12),
            "POSTGRES_AGENT_PASSWORD": "scaleagent" + secrets.token_hex(12),
            "JWT_SECRET_KEY": "scalejwt" + secrets.token_hex(24),
            "SCALE_WEBHOOK_TOKEN": self.webhook_token,
            # A fresh database has nothing to restore.
            "VIGIL_UPGRADE_ACK_VERSION": (REPO / "VERSION").read_text().strip(),
            "VIGIL_UPGRADE_ACK_REASON": "scale run: throwaway stack",
            # Intake is measured to the stored row. Triage and enrichment are a
            # model's speed, and there is no model here to call.
            "DAEMON_AUTO_TRIAGE": "false",
            "DAEMON_AUTO_ENRICH": "false",
            "DAEMON_AUTO_RESPONSE": "false",
        }
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
            if name in os.environ:
                self.env[name] = os.environ[name]

    # ---- docker -------------------------------------------------------------

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(
            ["docker", "compose", "--env-file", os.devnull, "-f", str(BASE),
             "-f", str(OVERRIDE), "--profile", "daemon", "-p", PROJECT, *args],
            env=self.env, capture_output=True, text=True,
        )
        if check and done.returncode:
            err = "\n".join(
                ln for ln in done.stderr.splitlines() if "level=warning" not in ln
            )
            raise StackFailed(
                f"docker compose {' '.join(args)[:120]}: {done.stdout[-1000:]}\n{err[-2000:]}"
            )
        return done

    def start(self) -> None:
        self.stop()  # a killed run leaves volumes holding its random passwords
        self.compose("build", "backend", "soc-daemon")
        self.compose("up", "-d", "--no-deps", "postgres", "redis")
        self.wait("Postgres and Redis", lambda: all(
            '"healthy"' in self.compose("ps", "--format", "json", svc).stdout
            for svc in ("postgres", "redis")
        ))
        self.compose("run", "--rm", "--no-deps", "upgrade")
        self.compose("up", "-d", "--no-deps", "backend", "soc-daemon")
        self.api_port = self.port("backend", 6987)
        self.intake_port = self.port("soc-daemon", 8081)
        self.wait("the API", lambda: self.call("GET", "/api/health", auth=False)[0] == 200)
        self.wait(
            "the daemon's webhook",
            lambda: _status(self.intake_port, "GET", "/health") == 200,
        )

    def stop(self, images: bool = False) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)
        if images:
            for name in IMAGES:
                subprocess.run(
                    ["docker", "image", "rm", "-f", f"{PROJECT}-{name}:local"],
                    capture_output=True, check=False,
                )

    def port(self, service: str, container_port: int) -> int:
        out = self.compose("port", service, str(container_port)).stdout.strip()
        return int(out.rsplit(":", 1)[1])

    def logs(self, service: str, tail: str = "all") -> str:
        done = self.compose("logs", "--no-color", "--tail", tail, service, check=False)
        return done.stdout + done.stderr

    def sql(self, query: str) -> str:
        return self.compose(
            "exec", "-T", "postgres", "psql", "-U", "deeptempo", "-d", "deeptempo_soc",
            "-tAc", query,
        ).stdout.strip()

    def wait(self, what: str, ready, timeout: float = 300.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if ready():
                    return
            except (OSError, http.client.HTTPException, StackFailed):
                pass
            time.sleep(2)
        raise StackFailed(f"{what} did not come up in {timeout:.0f} s")

    # ---- the API as a caller -------------------------------------------------

    def headers(self, auth: bool = True) -> dict[str, str]:
        """What every request carries: the token, and the CSRF pair writes need."""
        out = {"Cookie": f"csrf_token={self.csrf}", "X-CSRF-Token": self.csrf}
        if auth and self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def call(
        self, method: str, path: str, body: dict | None = None, auth: bool = True,
        bearer: str = "",
    ) -> tuple[int, dict | list | str]:
        headers = {**self.headers(auth), "Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        conn = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=120)
        try:
            conn.request(
                method, path, json.dumps(body) if body is not None else None, headers
            )
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text

    def sign_in(self) -> None:
        """The first admin, with MFA, then the service account and its token.

        Creating an account asks a person for a second factor, and a token can't
        mint a token, so a person signs in once. Sets ``token``.
        """
        code = ""

        def announced() -> bool:
            nonlocal code
            found = re.findall(
                r"First-run setup code:\s*([0-9a-fA-F]{32})", self.logs("backend")
            )
            code = found[-1] if found else ""
            return bool(code)

        self.wait("the first-run setup code", announced)
        password = "Sc" + secrets.token_urlsafe(24) + "!9"

        def expect(wanted: int, what: str, got: tuple[int, dict | list | str]):
            if got[0] != wanted:
                raise StackFailed(f"{what}: HTTP {got[0]} {str(got[1])[:300]}")
            return got[1]

        expect(201, "bootstrap", self.call("POST", "/api/auth/bootstrap", {
            "username": ADMIN, "email": f"{ADMIN}@example.com",
            "password": password, "setup_code": code,
        }, auth=False))
        session = expect(200, "login", self.call("POST", "/api/auth/login", {
            "username_or_email": ADMIN, "password": password,
        }, auth=False))
        person = session["access_token"]
        setup = expect(200, "MFA setup", self.call(
            "POST", "/api/auth/mfa/setup", bearer=person
        ))
        expect(200, "MFA verify", self.call("POST", "/api/auth/mfa/verify", {
            "code": totp(setup["secret"]), "password": password,
        }, bearer=person))
        me = expect(200, "the admin's own account", self.call(
            "GET", "/api/auth/me", bearer=person
        ))
        account = expect(201, "service account", self.call(
            "POST", "/api/users/service-accounts", {
                "name": SERVICE_ACCOUNT,
                "purpose": "The weekly scale run's reads and imports",
                "role_id": "role-admin",
                "accountable": me["user_id"],
            }, bearer=person,
        ))
        minted = expect(201, "token", self.call("POST", "/api/tokens", {
            "label": "scale run", "user_id": account["user_id"],
        }, bearer=person))
        self.token = minted["token"]


def _status(port: int, method: str, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path)
        return conn.getresponse().status
    finally:
        conn.close()
