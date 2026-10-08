"""V1-4 on a running stack: Vigil's own start path keeps Medic on its network.

Opt-in (it builds Medic's three images and runs a stack for a few minutes):

    VIGIL_MEDIC_COMPOSE_LIVE=1 python -m pytest tests/medic_compose/test_start_path_live.py

1. Medic enabled the documented way (`enable-compose.sh --secrets-only` writes
   the secrets and compose.env), then the stack up with the overlay: the real
   Medic images, every Vigil service a stub on its real network and port
   (`compose.test.yml`), under its own project (`medic-s11`, or
   VIGIL_MEDIC_COMPOSE_PROJECT) and its own image tags (`compose.s11.yml`).
2. The agent worker and the daemon stop, and come back the way `./start.sh`
   brings a container back: `ensure_container` from `scripts/lib.sh`, i.e.
   `dc up -d <service>`, with `.env`'s VIGIL_MEDIC_ENABLED="false" exported as
   `load_env` leaves it.
3. Medic still has its overlay: both are the same containers (not recreated),
   still on medic-net, and Medic reaches the worker's /readyz by name (not
   blind). An `up` of Medic through `dc` leaves it as it was, flag on.

Running `./start.sh` itself isn't possible here: it starts the host-native app
and its fixed `deeptempo-*` container names belong to the developer's own stack.
Every Compose call it makes goes through `dc` (test_dc_overlay.py pins that).

Mutation check: VIGIL_MEDIC_START_PATH_LIB names another lib.sh for step 2
(e.g. one whose `dc` drops the overlay); this test must then fail.
The project and its images are removed at the end; nothing outside is touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from tests.medic_compose.compose import ENABLE, HAVE_COMPOSE, REPO, clean_env

HERE = Path(__file__).resolve().parent
PROJECT = os.environ.get("VIGIL_MEDIC_COMPOSE_PROJECT", "medic-s11")
LIB = os.environ.get("VIGIL_MEDIC_START_PATH_LIB", str(REPO / "scripts" / "lib.sh"))
OVERRIDES = (HERE / "compose.test.yml", HERE / "compose.s11.yml")
IMAGES = (
    "vigil-medic:medic-s11",
    "vigil-medic-gateway:medic-s11",
    "vigil-medic-dockerproxy:medic-s11",
)
SERVICES = (
    "medic",
    "medic-gateway",
    "medic-dockerproxy",
    "backend",
    "agent-worker",
    "agent-serve",
    "soc-daemon",
    "redis",
    "postgres",
    "bifrost",
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

REACH_WORKER = r"""
import json, socket, urllib.error, urllib.request
out = {}
try:
    with urllib.request.urlopen("http://agent-worker:6990/readyz", timeout=10) as r:
        out["worker"] = r.status
except urllib.error.HTTPError as e:
    out["worker"] = e.code
except Exception as e:
    out["worker"] = type(e).__name__ + ": " + str(e)
try:
    socket.getaddrinfo("soc-daemon", 9091)
    out["daemon"] = "resolves"
except OSError as e:
    out["daemon"] = str(e)
print(json.dumps(out))
"""


class Stack:
    def __init__(self, home: Path) -> None:
        self.secrets_dir = home / "medic-secrets"
        self.env = clean_env(
            home,
            VIGIL_MEDIC_SECRETS_DIR=str(self.secrets_dir),
            MEDIC_S6_STUB_DIR=str(HERE),
            MEDIC_S6_CANARY="S11CANARY" + secrets.token_hex(8),
            AGENT_INTERNAL_TOKEN="S11TOKEN" + secrets.token_hex(8),
            COMPOSE_PROJECT_NAME=PROJECT,
            VIGIL_MEDIC_COMPOSE_OVERRIDE=":".join(str(f) for f in OVERRIDES),
        )

    def compose(self, *args: str, check: bool = True, timeout: float = 900):
        """Through this branch's `dc`, which adds Medic's overlay and settings
        (and the test overrides, VIGIL_MEDIC_COMPOSE_OVERRIDE) once compose.env
        exists: the rendering step 2 must keep."""
        # Never the default project: that is the developer's own deeptempo stack.
        assert self.env["COMPOSE_PROJECT_NAME"].startswith("medic-")
        done = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{REPO}/scripts/lib.sh"\n'
                'dc --profile medic --profile daemon "$@"',
                "dc",
                *args,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and done.returncode != 0:
            raise AssertionError(f"dc {' '.join(args)}: {done.stdout}{done.stderr}")
        return done

    def start_path(self, body: str) -> subprocess.CompletedProcess:
        """Step 2: Vigil's own scripts, with .env sourced the way load_env does."""
        return subprocess.run(
            ["bash", "-c", f'source "{LIB}"\nexport VIGIL_MEDIC_ENABLED=false\n{body}'],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )

    def cid(self, service: str) -> str:
        return self.compose("ps", "-a", "-q", service).stdout.strip()

    def docker(self, *args: str, input: str | None = None, check: bool = True):
        return subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=True,
            check=check,
            env=self.env,
            timeout=120,
        )

    def inspect(self, cid: str) -> dict:
        return json.loads(self.docker("inspect", cid).stdout)[0]


def _wait(what: str, fn, timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("home"))
    enable = subprocess.run(
        [str(ENABLE), "--secrets-only"],
        env=s.env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert enable.returncode == 0, enable.stdout + enable.stderr
    s.compose("down", "-v", "--remove-orphans", check=False)
    try:
        s.compose("build", "medic", "medic-gateway", "medic-dockerproxy", timeout=1800)
        s.compose("up", "-d", "--no-build", *SERVICES)
        _wait(
            "medic healthy",
            lambda: s.inspect(s.cid("medic"))["State"].get("Health", {}).get("Status")
            == "healthy",
            180,
        )
        yield s
    finally:
        logs = s.compose("logs", "--no-color", check=False).stdout
        (tmp_path_factory.getbasetemp() / "medic-s11-compose.log").write_text(logs)
        s.compose("down", "-v", "--remove-orphans", check=False)
        s.docker("image", "rm", *IMAGES, check=False)


def _medic_net(info: dict) -> bool:
    return f"{PROJECT}_medic-net" in info["NetworkSettings"]["Networks"]


def _reach(s: Stack) -> dict:
    out = s.docker("exec", "-i", s.cid("medic"), "python", "-", input=REACH_WORKER)
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def restarted(stack):
    s = stack
    before = {svc: s.cid(svc) for svc in ("agent-worker", "soc-daemon", "medic")}
    assert all(
        _medic_net(s.inspect(before[svc])) for svc in ("agent-worker", "soc-daemon")
    )
    assert isinstance(_reach(s)["worker"], int)
    names = {svc: s.inspect(cid)["Name"].lstrip("/") for svc, cid in before.items()}
    s.docker("stop", before["agent-worker"], before["soc-daemon"])
    done = s.start_path(
        f'ensure_container "{names["agent-worker"]}" agent-worker ""\n'
        f'ensure_container "{names["soc-daemon"]}" soc-daemon daemon\n'
        f'ensure_container "{names["medic"]}" medic medic\n'
        # A running Medic: ensure_container would skip it; `up` it through dc.
        "COMPOSE_PROFILES=medic dc up -d medic\n"
    )
    s.start_path_result = done
    return s, before


def test_start_path_runs_clean(restarted) -> None:
    s, _ = restarted
    done = s.start_path_result
    assert done.returncode == 0, done.stdout + done.stderr


def test_start_path_keeps_the_agents_on_medic_net(restarted) -> None:
    s, before = restarted
    for svc in ("agent-worker", "soc-daemon"):
        after = s.cid(svc)
        # Same container: Compose saw the config it was created with.
        assert after == before[svc], f"{svc} was recreated without Medic's overlay"
        info = s.inspect(after)
        assert info["State"]["Running"], svc
        assert _medic_net(info), f"{svc} is off medic-net"


def test_medic_still_reaches_the_worker_and_daemon(restarted) -> None:
    s, _ = restarted
    reach = _wait("Medic to reach the worker", lambda: _reach(s), 30)
    assert isinstance(reach["worker"], int), reach  # an HTTP answer, not a DNS error
    assert reach["daemon"] == "resolves", reach


def test_medic_itself_is_untouched_and_on(restarted) -> None:
    """compose.env's flag beats .env's exported "false" on the start path."""
    s, before = restarted
    assert s.cid("medic") == before["medic"]
    info = s.inspect(before["medic"])
    assert "VIGIL_MEDIC_ENABLED=true" in info["Config"]["Env"]
    assert info["State"]["Running"]
