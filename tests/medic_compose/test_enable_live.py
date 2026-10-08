"""V1 on a running Compose stack: Off, then one command, then Running.

Opt-in (it builds the backend image and runs a stack for a few minutes):

    VIGIL_MEDIC_COMPOSE_LIVE=1 python -m pytest tests/medic_compose/test_enable_live.py

1. A fresh install with Medic off (real backend, Postgres, db-seed, Redis; stubs
   for Bifrost, the backup and the agents, `compose.v1.yml`): the backend says
   ``off`` and no service account exists.
2. `scripts/medic/enable-compose.sh`, for real, under its own project
   (`medic-v1`, or VIGIL_MEDIC_COMPOSE_PROJECT).
3. The account exists (random name, Viewer, service account), the backend says
   ``unknown`` (V2 adds running/down), Medic's `check` exits 0, and the gateway
   logs in to the real backend with the generated password and serves a read.
4. Failed logins past the lockout threshold don't lock it out.
5. Canary: neither generated secret appears in the script's output, the
   rendered config, compose.env or any container's logs.
The project is torn down at the end; nothing outside it is touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from tests.medic_compose.compose import ENABLE, HAVE_COMPOSE, clean_env, compose_cmd

HERE = Path(__file__).resolve().parent
OVERRIDE = HERE / "compose.v1.yml"
PROJECT = os.environ.get("VIGIL_MEDIC_COMPOSE_PROJECT", "medic-v1")
INSTALL = (
    "postgres",
    "redis",
    "bifrost",
    "backup-pre-upgrade",
    "backend",
    "db-seed",
    "agent-worker",
    "agent-serve",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("VIGIL_MEDIC_COMPOSE_LIVE") != "1",
        reason="live Compose test: set VIGIL_MEDIC_COMPOSE_LIVE=1",
    ),
    pytest.mark.skipif(not HAVE_COMPOSE, reason="needs Docker with Compose v2"),
]

STATUS = (
    "from core.platform.medic_status import medic_status; "
    "print(medic_status().value)"
)
GATEWAY_READ = r"""
import json, urllib.request
def get(path):
    try:
        with urllib.request.urlopen("http://medic-gateway:8471" + path, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
print(json.dumps({"read": get("/api/federation/sources"), "status": get("/_gw/status")}))
"""
BAD_LOGINS = r"""
import json, sys, urllib.request
user, n = sys.argv[1], int(sys.argv[2])
codes = []
for _ in range(n):
    req = urllib.request.Request(
        "http://localhost:6987/api/auth/login",
        data=json.dumps({"username_or_email": user, "password": "wrong-" + "x" * 30}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
        codes.append(200)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
print(json.dumps(codes))
"""


class Stack:
    def __init__(self, home: Path) -> None:
        self.secrets_dir = home / "medic-secrets"
        self.env = clean_env(
            home,
            COMPOSE_PROJECT_NAME=PROJECT,
            VIGIL_MEDIC_SECRETS_DIR=str(self.secrets_dir),
            VIGIL_MEDIC_COMPOSE_OVERRIDE=str(OVERRIDE),
            MEDIC_V1_STUB_DIR=str(HERE),
            JWT_SECRET_KEY="V1JWT" + secrets.token_hex(24),
            AGENT_INTERNAL_TOKEN="V1TOKEN" + secrets.token_hex(8),
            POSTGRES_PASSWORD="V1PG" + secrets.token_hex(12),
        )
        if os.environ.get("VIGIL_MEDIC_DOCKER_GID"):
            self.env["VIGIL_MEDIC_DOCKER_GID"] = os.environ["VIGIL_MEDIC_DOCKER_GID"]

    def compose(self, *args: str, medic: bool = False, check: bool = True, **kw):
        files = (OVERRIDE,)
        cmd = compose_cmd(*(("medic",) if medic else ()), overlay=medic, files=files)
        if medic:
            cmd[2:4] = ["--env-file", str(self.secrets_dir / "compose.env")]
        return subprocess.run(
            [*cmd, "-p", PROJECT, *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            **kw,
        )

    def py(self, service: str, code: str, *argv: str, medic: bool = False) -> str:
        done = self.compose(
            "exec", "-T", service, "python", "-c", code, *argv, medic=medic
        )
        return done.stdout.strip()

    def sql(self, query: str) -> str:
        done = self.compose(
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
        )
        return done.stdout.strip()

    def wait(self, what, until, timeout: float = 300.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = what()
                if until(last):
                    return last
            except subprocess.CalledProcessError as e:
                last = e.stderr
            time.sleep(3)
        raise AssertionError(f"timed out; last: {last!r}")

    def secret(self, name: str) -> str:
        path = self.secrets_dir / name
        if os.access(path, os.R_OK):
            return path.read_text().strip()
        # Linux: handed to the container's uid by the script.
        return subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "-v",
                f"{self.secrets_dir}:/s:ro",
                "--entrypoint",
                "cat",
                "vigil-medic-gateway:local",
                f"/s/{name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("medic-v1-home"))
    try:
        s.compose("build", "backend")
        s.compose("up", "-d", *INSTALL)
        # db-seed seeds the roles once create_all has run.
        s.wait(
            lambda: s.sql("SELECT count(*) FROM roles WHERE role_id = 'role-viewer'"),
            lambda out: out == "1",
        )
        yield s
    finally:
        # With Medic's files when they exist, so its services go too.
        medic = (s.secrets_dir / "compose.env").exists()
        s.compose("down", "-v", "--remove-orphans", medic=medic, check=False)


@pytest.fixture(scope="module")
def before(stack):
    return {
        "status": stack.wait(
            lambda: stack.py("backend", STATUS), lambda o: o in ("off", "unknown")
        ),
        "accounts": stack.sql("SELECT count(*) FROM users WHERE service_account"),
    }


@pytest.fixture(scope="module")
def enabled(stack, before):
    done = subprocess.run(
        [str(ENABLE)],
        env=stack.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def test_flag_off_status_off_and_no_account(before) -> None:
    assert before == {"status": "off", "accounts": "0"}


def test_the_account_exists_as_a_random_viewer_service_account(stack, enabled) -> None:
    name = stack.secret("viewer_username")
    row = stack.sql(
        "SELECT username, role_id, service_account, is_active FROM users "
        "WHERE service_account"
    )
    assert row == f"{name}|role-viewer|t|t"


def test_backend_status_is_unknown_once_on(stack, enabled) -> None:
    assert stack.py("backend", STATUS) == "unknown"


def test_medic_is_running(stack, enabled) -> None:
    health = stack.wait(
        lambda: stack.compose("ps", "--format", "json", "medic", medic=True).stdout,
        lambda out: '"Health":"healthy"' in out.replace(" ", ""),
        timeout=180,
    )
    assert "healthy" in health
    done = stack.compose(
        "exec",
        "-T",
        "medic",
        "python",
        "-m",
        "services.medic",
        "check",
        medic=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_gateway_logs_in_and_serves_a_read(stack, enabled) -> None:
    out = json.loads(stack.py("medic", GATEWAY_READ, medic=True))
    assert out["read"][0] == 200, out
    assert json.loads(out["status"][1])["state"] == "ok", out


def test_failed_logins_do_not_lock_the_service_account(stack, enabled) -> None:
    name = stack.secret("viewer_username")
    # The login limiter allows 5/min per IP; the threshold is 5.
    codes = json.loads(stack.py("backend", BAD_LOGINS, name, "5"))
    assert codes == [401] * 5, codes
    row = stack.sql(
        f"SELECT failed_login_count, locked_until IS NULL FROM users WHERE username = '{name}'"
    )
    assert row == "5|t"
    # The gateway (another IP) can still log in: restart it so it logs in afresh.
    stack.compose("restart", "medic-gateway", medic=True)
    out = stack.wait(
        lambda: json.loads(stack.py("medic", GATEWAY_READ, medic=True)),
        lambda o: o["read"][0] == 200,
        timeout=60,
    )
    assert json.loads(out["status"][1])["state"] == "ok"


def test_rerun_is_idempotent(stack, enabled) -> None:
    name = stack.secret("viewer_username")
    done = subprocess.run(
        [str(ENABLE)],
        env=stack.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    assert done.returncode == 0, done.stderr
    assert (
        stack.sql("SELECT count(*) FROM users WHERE service_account AND is_active")
        == "1"
    )
    assert stack.secret("viewer_username") == name


def test_canary_no_secret_in_output_config_or_logs(stack, enabled) -> None:
    values = [stack.secret("viewer_password"), stack.secret("api_key")]
    config = stack.compose("config", medic=True).stdout
    logs = stack.compose("logs", "--no-color", medic=True).stdout
    settings = (stack.secrets_dir / "compose.env").read_text()
    for value in values:
        assert len(value) == 64
        for where, text in [
            ("script stdout", enabled.stdout),
            ("script stderr", enabled.stderr),
            ("compose config", config),
            ("container logs", logs),
            ("compose.env", settings),
        ]:
            assert value not in text, where
