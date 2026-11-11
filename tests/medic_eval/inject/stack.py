"""The Compose project the fault injectors break (compose.eval.yml).

A real backend, Postgres, upgrade, Redis, soc-daemon, agent-serve and
agent-worker, with Medic, its gateway and Docker proxy once scripts/medic/
enable-compose.sh has run, and auth on (DEV_MODE off), under a `medic-eval-*`
project of its own (VIGIL_MEDIC_EVAL_PROJECT overrides the name;
compose_project() refuses anything else). Nothing touches a deeptempo-*
container, port or volume. Secrets are minted per run.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path

from tests.medic_compose.compose import (
    ENABLE,
    REPO,
    clean_env,
    compose_cmd,
    compose_project,
    require_compose_project,
)
from tests.medic_eval.sample import SERVICES as SAMPLED

HERE = Path(__file__).resolve().parent
OVERRIDE = HERE / "compose.eval.yml"
STUB_DIR = REPO / "tests" / "medic_compose"
PROJECT_ENV = "VIGIL_MEDIC_EVAL_PROJECT"
IMAGES = ("backend", "daemon", "agent")  # <project>-<name>:local, see compose.eval.yml
# enable-compose.sh builds these and reads the gateway's by tag.
MEDIC_IMAGES = ("vigil-medic:local", "vigil-medic-gateway:local", "vigil-medic-dockerproxy:local")
ADMIN_USER = "evaladmin"
# core/auth/bootstrap_code.py SETUP_CODE_LOG_MARK (the harness runs on the host, so no import).
SETUP_CODE_LOG_MARK = "First-run setup code:"
INSTALL = (
    "postgres",
    "redis",
    "bifrost",
    "backup-pre-upgrade",
    "mcp-runner-token",
    "metrics-token",
    "mcp-runner",
    "backend",
    "upgrade",
    "siem-stub",
    "splunk-stub",
    "soc-daemon",
    "llm-worker",
    "agent-worker",
    "agent-serve",
)
# The poll interval the daemon runs at, so "no poll yet" is seen in seconds.
POLL_S = 15

# One HTTP call from inside the network: prints [status, body]; status 0 is a
# connection that was refused or never answered.
HTTP = r"""
import json, sys, urllib.request, urllib.error
method, url, token, body = sys.argv[1:5]
headers = {"Content-Type": "application/json", **json.loads(sys.argv[5] if len(sys.argv) > 5 else "{}")}
if token:
    headers["Authorization"] = "Bearer " + token
req = urllib.request.Request(url, data=body.encode() if body else None, method=method, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        out = [r.status, r.read().decode()]
except urllib.error.HTTPError as e:
    out = [e.code, e.read().decode()]
except Exception as e:
    out = [0, str(e)]
print(json.dumps(out))
"""
# The same call from a Node service, with the token it was started with.
NODE_FETCH = (
    "fetch(process.argv[1], {headers: {authorization: 'Bearer ' + (process.argv[2] || "
    "process.env.AGENT_INTERNAL_TOKEN || '')}}).then(r => console.log(r.status), "
    "() => console.log(0))"
)


CODE_FRAME = re.compile(r"^\s*\d+ \| ")  # BuildKit's Dockerfile excerpt on a failed step


def _tail(text: str, limit: int) -> str:
    """The last lines of `text` within `limit` chars, each long line cut in its middle."""
    lines = [ln if len(ln) <= 300 else f"{ln[:100]} … {ln[-160:]}" for ln in text.splitlines()]
    out: list[str] = []
    for ln in reversed(lines):
        if sum(len(x) + 1 for x in out) + len(ln) > limit:
            break
        out.append(ln)
    return "\n".join(reversed(out))


def docker_api(path: str) -> dict:
    """GET on the Docker Engine API over its Unix socket (DOCKER_HOST, else the default)."""
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if not host.startswith("unix://"):
        raise RuntimeError(f"the Docker API is read over a Unix socket, not {host}")
    conn = http.client.HTTPConnection("docker", timeout=30)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.settimeout(30)
    try:
        conn.sock.connect(host[len("unix://"):])
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"docker API {path}: {resp.status} {body[:300]!r}")
    return json.loads(body)


class ComposeFailed(AssertionError):
    pass


class Stack:
    backend_host = "backend"  # the backend's name on the network

    def __init__(self, home: Path, project: str | None = None) -> None:
        # Before any docker call.
        self.project = (
            compose_project(PROJECT_ENV, "medic-eval")
            if project is None
            else require_compose_project(project)
        )
        token = secrets.token_hex(16)
        self.secrets_dir = home / "medic-secrets"
        self.admin_password = "Ev" + secrets.token_urlsafe(24) + "!9"
        self.csrf = secrets.token_urlsafe(32)  # the double-submit pair
        self.bearer = ""
        self.medic = False  # Medic's overlay and profile join every call once enabled
        self.good_webhook_token = "EVALHOOK" + secrets.token_hex(12)
        self.agent_token = "EVALAGENT" + secrets.token_hex(12)
        self.env = clean_env(
            home,
            COMPOSE_PROJECT_NAME=self.project,
            MEDIC_EVAL_STUB_DIR=str(STUB_DIR),
            MEDIC_EVAL_SITE=str(HERE / "sitecustomize.py"),
            VIGIL_UPGRADE_ACK_VERSION=(REPO / "VERSION").read_text().strip(),
            VIGIL_UPGRADE_ACK_REASON="medic fault injector live test",
            REDIS_PASSWORD="EVALREDIS" + token,
            JWT_SECRET_KEY="EVALJWT" + secrets.token_hex(24),
            POSTGRES_PASSWORD="EVALPG" + secrets.token_hex(12),
            AGENT_INTERNAL_TOKEN=self.agent_token,
            MEDIC_EVAL_AGENT_TOKEN=self.agent_token,
            MEDIC_EVAL_WEBHOOK_TOKEN=self.good_webhook_token,
            # Auth on: Medic's sensors read the API only with DEV_MODE off.
            DEV_MODE="false",
            VIGIL_MEDIC_SECRETS_DIR=str(self.secrets_dir),
            VIGIL_MEDIC_COMPOSE_OVERRIDE=str(OVERRIDE),
            VIGIL_MEDIC_DOTENV=os.devnull,  # not a developer's .env
            DAEMON_SPLUNK_POLL_INTERVAL=str(POLL_S),
        )
        if os.environ.get("VIGIL_MEDIC_DOCKER_GID"):
            self.env["VIGIL_MEDIC_DOCKER_GID"] = os.environ["VIGIL_MEDIC_DOCKER_GID"]
        # What the push client presents (I-5.b); the daemon's own token is in env.
        self.pusher_token = self.good_webhook_token
        # Per-cell state a second inject or a later verify needs.
        self.active: set[str] = set()
        self.marks: dict[str, int] = {}

    # ---- docker -------------------------------------------------------------

    def compose(
        self, *args: str, check: bool = True, **kw
    ) -> subprocess.CompletedProcess:
        # Without the overlay and profile on every call, Docker drops the agents
        # off medic-net at the next `up`.
        profiles = ("daemon", "medic") if self.medic else ("daemon",)
        cmd = compose_cmd(*profiles, overlay=self.medic, files=(OVERRIDE,))
        done = subprocess.run(
            [*cmd, "-p", self.project, *args],
            env=self.env,
            capture_output=True,
            text=True,
            **kw,
        )
        if check and done.returncode:
            # Compose's own words, not just the exit status. A build's step output
            # (the failing step's last lines) is on stdout; stderr adds unset-variable
            # warnings and a Dockerfile code frame (both dropped) and the whole RUN
            # command (long lines are cut in the middle, keeping the exit code).
            err = "\n".join(
                ln for ln in done.stderr.splitlines()
                if ln.strip() and not CODE_FRAME.match(ln) and "level=warning" not in ln
            )
            raise ComposeFailed(
                f"docker compose {' '.join(args)[:200]}: "
                f"{_tail(done.stdout, 1000)}\n{_tail(err, 2000)}".strip()
            )
        return done

    def up(self, *services: str) -> None:
        """Create or recreate what a changed environment touches; no-op if nothing changed."""
        self.compose("up", "-d", "--no-deps", *services)

    def recreate(self, *services: str) -> None:
        self.compose("up", "-d", "--no-deps", "--force-recreate", *services)

    def start(self) -> None:
        # A run that was killed leaves containers and volumes holding its random
        # passwords (Medic's too, which `down` skips without its profile).
        stale = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"],
            capture_output=True, text=True, check=False,
        ).stdout.split()
        if stale:
            subprocess.run(["docker", "rm", "-f", *stale], capture_output=True, check=False)
        self.compose("down", "-v", "--remove-orphans", check=False)
        self.compose("build", "backend", "soc-daemon", "agent-serve")
        self.compose("up", "-d", *INSTALL)
        self.wait(
            lambda: self.sql(
                "SELECT count(*) FROM roles WHERE role_id = 'role-viewer'"
            ),
            lambda out: out == "1",
        )
        self.wait(
            lambda: self.http("GET", "backend", 6987, "/api/health")[0],
            lambda c: c == 200,
        )
        self.wait_daemon()
        for svc in ("agent-serve", "agent-worker", "llm-worker"):
            self.wait(
                lambda s=svc: self.compose("ps", "--format", "json", s).stdout,
                lambda o: '"healthy"' in o,
            )
        self.bootstrap_admin()
        self.enable_medic()

    def bootstrap_admin(self) -> None:
        """The first-run flow: a fresh database has no admin (Medic's Viewer is a service account)."""
        # Read the code from the API's log as an operator does; a bare `python -c` has no
        # database for the key ring. Announced by a background task, so wait for it.
        out = self.wait(
            lambda: self.logs("backend"), lambda out: SETUP_CODE_LOG_MARK in out
        )
        line = [ln for ln in out.splitlines() if SETUP_CODE_LOG_MARK in ln][-1]
        code = line.split(SETUP_CODE_LOG_MARK, 1)[1].strip()
        status, body = self.api(
            "POST",
            "/api/auth/bootstrap",
            {
                "username": ADMIN_USER,
                "email": "evaladmin@example.com",
                "password": self.admin_password,
                "setup_code": code,
            },
            auth=False,
        )
        assert status == 201, (status, body)

    def enable_medic(self) -> None:
        """The shipped enable script, for real: secrets, networks, Viewer account, the three services."""
        done = subprocess.run(
            [str(ENABLE)], env=self.env, capture_output=True, text=True,
            check=False, timeout=1800,
        )
        if done.returncode:
            raise ComposeFailed(f"enable-compose.sh: {done.stdout[-1500:]}{done.stderr[-1500:]}")
        # What compose.env holds, as the environment every later call needs.
        for line in (self.secrets_dir / "compose.env").read_text().splitlines():
            if line.startswith("VIGIL_MEDIC_") and "=" in line:
                k, v = line.split("=", 1)
                self.env[k] = v
        self.medic = True
        self.wait(
            lambda: self.compose("ps", "--format", "json", "medic").stdout,
            lambda o: '"healthy"' in o,
            timeout=420,
        )
        self.wait(
            lambda: self.http("GET", "backend", 6987, "/api/health")[0],
            lambda c: c == 200,
        )

    def stop(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)
        for image in (*(f"{self.project}-{n}:local" for n in IMAGES), *MEDIC_IMAGES):
            subprocess.run(
                ["docker", "image", "rm", "-f", image], capture_output=True, check=False
            )

    # ---- what nightly.py reads from outside Medic (HelmStack has its own) --------

    medic_key_file = "/run/secrets/medic_api_key"
    medic_services = SAMPLED
    vigil_services = ("backend", "soc-daemon", "agent-serve", "agent-worker", "llm-worker", "postgres", "redis")

    def medic_exec(self, *argv: str) -> str:
        """stdout of a command run in the Medic container ('' if it fails)."""
        return self.compose("exec", "-T", "medic", *argv, check=False).stdout

    def _counters(self) -> dict[str, tuple[int, int, float]]:
        """{Medic service: (cumulative CPU ns, bytes in use, monotonic s when read)}: the Docker API's one-shot stats."""
        ps = subprocess.run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={self.project}",
             "--format", '{{.ID}} {{.Label "com.docker.compose.service"}}'],
            capture_output=True, text=True, check=False,
        ).stdout
        rows = {}
        for cid, _, svc in (line.partition(" ") for line in ps.splitlines()):
            if svc in SAMPLED:
                at = time.monotonic()
                st = docker_api(f"/containers/{cid}/stats?stream=false&one-shot=true")
                at = (at + time.monotonic()) / 2  # each container on its own clock: the reads are sequential
                mem = st["memory_stats"]
                inactive = mem.get("stats", {}).get("inactive_file", mem.get("stats", {}).get("total_inactive_file", 0))
                rows[svc] = (st["cpu_stats"]["cpu_usage"]["total_usage"], mem["usage"] - inactive, at)  # docker stats' MEM USAGE
        return rows

    def usage(self) -> dict[str, tuple[float, float]]:
        """{Medic service: (vCPU, MiB)}: the CPU counter read twice a couple of seconds apart, as HelmStack does."""
        first = self._counters()
        time.sleep(2)
        second = self._counters()
        return {
            n: (max(c1 - first[n][0], 0) / 1e9 / (at - first[n][2]), mem / 1024**2)
            for n, (c1, mem, at) in second.items() if n in first
        }

    def medic_limits(self) -> dict:
        """The medic container's limits (None: unset) and whether it was killed for memory or restarted."""
        cid = self.compose("ps", "-aq", "medic").stdout.split()[0]
        info = docker_api(f"/containers/{cid}/json")
        host = info["HostConfig"]
        return {
            "cpu_vcpu": host["NanoCpus"] / 1e9 or None,
            "memory_mib": host["Memory"] / 1024**2 or None,
            "restarts": info["RestartCount"],
            "oom_killed": info["State"]["OOMKilled"],
        }

    def not_running(self) -> list[str]:
        """Vigil's services that are not running."""
        return [
            svc for svc in self.vigil_services
            if not self.compose("ps", "--status", "running", "-q", svc, check=False).stdout.strip()
        ]

    # ---- reads --------------------------------------------------------------

    def py(self, service: str, code: str, *argv: str) -> str:
        return self.compose(
            "exec", "-T", service, "python", "-c", code, *argv
        ).stdout.strip()

    def sql(self, query: str) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "deeptempo",
            "-d",
            "deeptempo_soc",
            "-tAc",
            query,
        ).stdout.strip()

    def http(
        self,
        method: str,
        host: str,
        port: int,
        path: str,
        token: str = "",
        body: str = "",
        headers: dict | None = None,
    ) -> tuple[int, str]:
        """One request from the backend container to a service on the network."""
        out = self.py(
            "backend", HTTP, method, f"http://{host}:{port}{path}", token, body,
            json.dumps(headers or {}),
        )
        status, text = json.loads(out)
        return status, text

    def login(self) -> None:
        """Sign in as the admin the first-run flow made, sending the shipped CSRF pair."""
        status, text = self.http(
            "POST", "backend", 6987, "/api/auth/login",
            body=json.dumps({"username_or_email": ADMIN_USER, "password": self.admin_password}),
            headers=self._csrf(),
        )
        assert status == 200, (status, text)
        self.bearer = json.loads(text)["access_token"]

    def _csrf(self) -> dict:
        # Vigil compares the cookie with the header (double submit).
        return {"Cookie": f"csrf_token={self.csrf}", "X-CSRF-Token": self.csrf}

    def api(
        self, method: str, path: str, body: dict | None = None, auth: bool = True
    ) -> tuple[int, dict | list | str]:
        """The backend's API as the admin; signs in on first use and again after a 401."""
        for attempt in (0, 1):
            if auth and not self.bearer:
                self.login()
            status, text = self.http(
                method, "backend", 6987, path,
                token=self.bearer if auth else "",
                body=json.dumps(body) if body is not None else "",
                headers=self._csrf(),
            )
            if status == 401 and auth and not attempt:
                self.bearer = ""  # expired: one fresh login
                continue
            break
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def push(self, token: str | None = None) -> int:
        """An empty batch to the daemon's webhook: authenticated or refused, nothing ingested."""
        token = self.pusher_token if token is None else token
        return self.http("POST", "soc-daemon", 8081, "/ingest", token, "[]")[0]

    def node(self, service: str, *argv: str) -> int:
        out = self.compose(
            "exec", "-T", service, "node", "-e", NODE_FETCH, *argv
        ).stdout.strip()
        return int(out)

    def logs(self, service: str) -> str:
        done = self.compose("logs", "--no-color", service, check=False)
        return done.stdout + done.stderr

    def set_integration(self, integration_id: str, config: dict, enabled: bool) -> None:
        """What Settings -> Integrations saves: POST /api/config/integrations."""
        status, body = self.api(
            "POST",
            "/api/config/integrations",
            {
                "enabled_integrations": [integration_id] if enabled else [],
                "integrations": {integration_id: config},
            },
        )
        assert status == 200, (status, body)

    def wait_daemon(self) -> None:
        """The daemon is up and its poller has started (it answers the webhook, one way or the other)."""
        self.wait(
            lambda: self.http("POST", "soc-daemon", 8081, "/ingest", "", "[]")[0],
            lambda c: c in (401, 503),
        )
        self.wait(
            lambda: self.logs("soc-daemon"), lambda out: "Data poller starting" in out
        )

    def wait(self, what, until, timeout: float = 240.0, every: float = 2.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = what()
                if until(last):
                    return last
            except (ComposeFailed, ValueError) as e:
                last = str(e)
            time.sleep(every)
        raise AssertionError(f"timed out; last: {last!r}")
