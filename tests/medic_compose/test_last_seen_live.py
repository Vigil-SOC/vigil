"""V2 + S9 on a running Compose stack: Off, Running, killed → Down, kept, back.

Opt-in (it builds the backend and Medic images and runs a stack for about 12
minutes):

    VIGIL_MEDIC_COMPOSE_LIVE=1 python -m pytest tests/medic_compose/test_last_seen_live.py

The real backend, Postgres, db-seed, Redis, gateway and **Medic** (S9 serves
`GET /v1/status`; V2 ran this against a stub); V1's stubs for the rest
(`compose.v1.yml`). Real timings: one poll a minute, Down at 5 min.

1. Flag off: the backend says ``off`` and writes no row.
2. `scripts/medic/enable-compose.sh`: the backend polls the real Medic through
   the gateway's inbound listener with the API key and says ``running``.
3. Medic stopped: ``down`` within 300 s of the kill, failure kind ``refused``.
4. The backend restarted: still ``down``, same ``first_failed_at``.
5. Medic started again: ``running``, the failure cleared, its restart counted.
Own project (`medic-s9`), torn down at the end; nothing outside it is touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

import pytest

from tests.medic_compose.compose import ENABLE, HAVE_COMPOSE
from tests.medic_compose.test_enable_live import INSTALL, OVERRIDE, Stack

PROJECT = os.environ.get("VIGIL_MEDIC_LAST_SEEN_PROJECT", "medic-s9")

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
    "from core.storage.connection import init_database; "
    "init_database(create_tables=False); "
    "from core.platform.medic_last_seen import current_status; "
    "print(current_status().value)"
)
ROW = (
    "SELECT coalesce(last_seen_at::text, '-'), coalesce(first_failed_at::text, '-'), "
    "coalesce(failure_kind, '-'), coalesce(status_snapshot->>'state', '-') "
    "FROM medic_last_seen"
)
SNAPSHOT = "SELECT coalesce(status_snapshot::text, '-') FROM medic_last_seen"
# Medic's own C5 states (X2 `Status.state`); down/off/unknown are the backend's.
MEDIC_STATES = (
    "running",
    "degraded",
    "not_recording",
    "blind",
    "starting",
    "crash_looping",
)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(
        tmp_path_factory.mktemp("medic-v2-home"),
        project=PROJECT,
        files=(OVERRIDE,),
    )
    try:
        s.compose("build", "backend")
        s.compose("up", "-d", *INSTALL)
        s.wait(
            lambda: s.sql("SELECT count(*) FROM roles WHERE role_id = 'role-viewer'"),
            lambda out: out == "1",
        )
        yield s
    finally:
        s.compose("down", "-v", "--remove-orphans", medic=True, check=False)


def _status(stack) -> str:
    return stack.py("backend", STATUS)


def _log(stack, what: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {what}: {stack.sql(ROW)!r}", flush=True)


@pytest.fixture(scope="module")
def off(stack):
    stack.wait(lambda: _status(stack), lambda o: o == "off")
    time.sleep(70)  # past a poll interval: a poller would have written by now
    return {
        "status": _status(stack),
        "rows": stack.sql("SELECT count(*) FROM medic_last_seen"),
    }


@pytest.fixture(scope="module")
def running(stack, off):
    done = subprocess.run(
        [str(ENABLE)],
        env=stack.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    status = stack.wait(lambda: _status(stack), lambda o: o == "running", timeout=180)
    _log(stack, "running")
    return status


@pytest.fixture(scope="module")
def killed(stack, running):
    stack.compose("stop", "medic", medic=True)
    t_kill = time.monotonic()
    stack.wait(lambda: _status(stack), lambda o: o == "down", timeout=420)
    elapsed = time.monotonic() - t_kill
    _log(stack, f"down after {elapsed:.0f}s")
    return {"elapsed": elapsed, "row": stack.sql(ROW)}


def test_flag_off_reads_off_and_writes_nothing(off) -> None:
    assert off == {"status": "off", "rows": "0"}


def test_enabled_reads_running_through_the_gateway(stack, running) -> None:
    _, failed, kind, state = stack.sql(ROW).split("|")
    assert (failed, kind) == ("-", "-")
    # The real Medic's own view, typed by the backend (V2-9).
    assert state in MEDIC_STATES
    snap = json.loads(stack.sql(SNAPSHOT))
    assert re.fullmatch(r"mi_[0-9a-f]{16}", snap["instance_id"])
    assert snap["api_version"] == "1.0" and snap["cycle"] >= 0
    # S9-1: Medic listens only on medic-private, the gateway's network, never on
    # medic-net beside the agents (K1 §6 G3).
    medic_log = stack.compose("logs", "--no-color", "medic", medic=True).stdout
    assert re.search(r"listening on [0-9.]+:8470", medic_log), medic_log
    assert "listening on 0.0.0.0" not in medic_log
    probe = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('medic', 8470), timeout=3)\n"
        "    print('open')\n"
        "except OSError as e:\n"
        "    print('closed', type(e).__name__)"
    )
    assert stack.py("agent-worker", probe, medic=True).startswith("closed")
    # The poll went backend -> gateway inbound -> Medic, with the key.
    logs = stack.compose("logs", "--no-color", "medic-gateway", medic=True).stdout
    assert '"dir": "inbound"' in logs or '"dir":"inbound"' in logs
    assert "/v1/status" in logs


def test_killed_medic_reads_down_within_five_minutes(killed) -> None:
    assert killed["elapsed"] <= 300, killed
    _, failed, kind, _ = killed["row"].split("|")
    assert failed != "-" and kind == "refused"


def test_a_backend_restart_keeps_the_state(stack, killed) -> None:
    before = stack.sql(ROW)
    stack.compose("restart", "backend", medic=True)
    stack.wait(lambda: _status(stack), lambda o: o in ("down", "unknown", "running"))
    assert _status(stack) == "down"
    assert stack.sql(ROW).split("|")[1] == before.split("|")[1]  # first_failed_at


def test_medic_back_reads_running_and_clears_the_failure(stack, killed) -> None:
    stack.compose("start", "medic", medic=True)
    stack.wait(lambda: _status(stack), lambda o: o == "running", timeout=180)
    _log(stack, "recovered")
    _, failed, kind, _ = stack.sql(ROW).split("|")
    assert (failed, kind) == ("-", "-")
    # Medic knows it was stopped and started again (C5 §5.4 card fields).
    snap = json.loads(stack.sql(SNAPSHOT))
    assert snap["restarts_24h"] >= 1
    assert snap["last_exit"]["reason"] == "clean"


def test_the_api_key_is_in_no_log(stack, killed) -> None:
    key = stack.secret("api_key")
    logs = stack.compose("logs", "--no-color", medic=True).stdout
    assert key not in logs
