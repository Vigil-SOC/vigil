"""The Compose project the fault injectors break (compose.eval.yml).

A real backend, Postgres, upgrade, Redis, soc-daemon, agent-serve and
agent-worker, under a `medic-eval-*` project of its own (VIGIL_MEDIC_EVAL_PROJECT
overrides the name; compose_project() refuses anything else). Nothing touches a
deeptempo-* container, port or volume. Secrets are minted per run.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path

from tests.medic_compose.compose import (
    REPO,
    clean_env,
    compose_cmd,
    compose_project,
    require_compose_project,
)

HERE = Path(__file__).resolve().parent
OVERRIDE = HERE / "compose.eval.yml"
STUB_DIR = REPO / "tests" / "medic_compose"
PROJECT_ENV = "VIGIL_MEDIC_EVAL_PROJECT"
IMAGES = ("backend", "daemon", "agent")  # <project>-<name>:local, see compose.eval.yml
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
headers = {"Content-Type": "application/json"}
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


class ComposeFailed(AssertionError):
    pass


class Stack:
    def __init__(self, home: Path, project: str | None = None) -> None:
        # Before any docker call.
        self.project = (
            compose_project(PROJECT_ENV, "medic-eval")
            if project is None
            else require_compose_project(project)
        )
        token = secrets.token_hex(16)
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
            # Auth off: the injectors drive the backend's own API without a login.
            DEV_MODE="true",
            DAEMON_SPLUNK_POLL_INTERVAL=str(POLL_S),
        )
        # What the push client presents (I-5.b); the daemon's own token is in env.
        self.pusher_token = self.good_webhook_token
        # Per-cell state a second inject or a later verify needs.
        self.active: set[str] = set()
        self.marks: dict[str, int] = {}

    # ---- docker -------------------------------------------------------------

    def compose(
        self, *args: str, check: bool = True, **kw
    ) -> subprocess.CompletedProcess:
        cmd = compose_cmd("daemon", overlay=False, files=(OVERRIDE,))
        done = subprocess.run(
            [*cmd, "-p", self.project, *args],
            env=self.env,
            capture_output=True,
            text=True,
            **kw,
        )
        if check and done.returncode:
            # Compose's own words, not just the exit status.
            raise ComposeFailed(
                f"docker compose {' '.join(args)[:200]}: {done.stderr[-1500:]}"
            )
        return done

    def up(self, *services: str) -> None:
        """Create or recreate what a changed environment touches; no-op if nothing changed."""
        self.compose("up", "-d", "--no-deps", *services)

    def recreate(self, *services: str) -> None:
        self.compose("up", "-d", "--no-deps", "--force-recreate", *services)

    def start(self) -> None:
        # A run that was killed leaves volumes holding its random passwords.
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

    def stop(self) -> None:
        self.compose("down", "-v", "--remove-orphans", check=False)
        for name in IMAGES:
            subprocess.run(
                ["docker", "image", "rm", "-f", f"{self.project}-{name}:local"],
                capture_output=True,
                check=False,
            )

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
    ) -> tuple[int, str]:
        """One request from the backend container to a service on the network."""
        out = self.py(
            "backend", HTTP, method, f"http://{host}:{port}{path}", token, body
        )
        status, text = json.loads(out)
        return status, text

    def api(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict | list | str]:
        status, text = self.http(
            method,
            "backend",
            6987,
            path,
            body=json.dumps(body) if body is not None else "",
        )
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
