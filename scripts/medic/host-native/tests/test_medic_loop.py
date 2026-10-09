"""medic-loop.sh: Medic's host-native restart loop (C5 ENG #3).

Driven as a subprocess, the way start.sh runs it, against two kinds of child:
a stub "python" (a shell script whose behaviour a file in the data dir sets),
and the real Medic from the interpreter running these tests.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
LOOP = HERE.parent / "medic-loop.sh"
REPO = HERE.parents[3]

# Stands in for the venv's python. env -i strips everything but the loop's own
# allowlist, so it is steered by files in the data dir, never by env.
STUB_PYTHON = r"""#!/bin/bash
d="$VIGIL_MEDIC_DATA_DIR"
echo "$(date +%s) $$ $*" >> "$d/starts"
env > "$d/env.$$"
case "$(cat "$d/mode" 2>/dev/null)" in
  exit3) exit 3 ;;
  run)
    trap 'echo "TERM $$" >> "$d/termed"; exit 0' TERM
    while :; do sleep 0.1; done ;;
esac
exit 0
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(mode=0o700)
    (tmp_path / "app").mkdir()
    py = tmp_path / "python"
    py.write_text(STUB_PYTHON)
    py.chmod(0o755)
    return tmp_path


def _args(box: Path, *extra: str, python: str | None = None) -> list[str]:
    return [
        "bash",
        str(LOOP),
        "--python",
        python or str(box / "python"),
        "--app",
        str(box / "app"),
        "--data-dir",
        str(box / "data"),
        *extra,
    ]


def _start(box: Path, *extra: str, **kw) -> subprocess.Popen[str]:
    log = open(box / "loop.log", "w")  # noqa: SIM115 - closed with the process
    return subprocess.Popen(
        _args(box, *extra, **kw),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "JWT_SECRET_KEY": "leak-me", "POSTGRES_PASSWORD": "pw"},
    )


def _starts(box: Path) -> list[list[str]]:
    p = box / "data" / "starts"
    return [ln.split() for ln in p.read_text().splitlines()] if p.exists() else []


def _wait_for(pred, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def _stop(proc: subprocess.Popen[str]) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=15)


def test_probe_lists_only_the_paths_this_user_can_read(tmp_path: Path) -> None:
    readable = tmp_path / "readable"
    readable.write_text("x")
    secret = tmp_path / "secret"
    secret.write_text("x")
    secret.chmod(0)
    out = subprocess.run(
        ["bash", str(LOOP), "--probe", str(readable), str(secret), str(tmp_path / "x")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == [str(readable)]


def test_child_env_is_an_allowlist(box: Path) -> None:
    """Check 9, host-native half: no Vigil credential reaches Medic's env."""
    (box / "data" / "mode").write_text("run")
    proc = _start(box)
    try:
        _wait_for(lambda: _starts(box))
        pid = _starts(box)[0][1]
        _wait_for(lambda: (box / "data" / f"env.{pid}").exists())
        env = dict(
            ln.split("=", 1)
            for ln in (box / "data" / f"env.{pid}").read_text().splitlines()
            if "=" in ln
        )
    finally:
        _stop(proc)
    assert "JWT_SECRET_KEY" not in env and "POSTGRES_PASSWORD" not in env
    assert env["VIGIL_MEDIC_ENABLED"] == "true"
    assert env["VIGIL_MEDIC_DATA_DIR"] == str(box / "data")
    assert env["PYTHONPATH"] == str(box / "app")
    # L49: a host-native Medic says so, and reads the worker on loopback.
    assert env["VIGIL_MEDIC_INSTALL_SHAPE"] == "start_sh"
    assert env["VIGIL_MEDIC_AGENT_WORKER_ADDR"] == "127.0.0.1:6990"
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "VIGIL_MEDIC_ENABLED",
        "VIGIL_MEDIC_DATA_DIR",
        "VIGIL_MEDIC_INSTALL_SHAPE",
        "VIGIL_MEDIC_AGENT_WORKER_ADDR",
        # Set by the shells themselves, not passed through.
        "PWD",
        "SHLVL",
        "_",
        "OLDPWD",
    }


def _child_env(box: Path, *extra: str) -> dict[str, str]:
    (box / "data" / "mode").write_text("run")
    proc = _start(box, *extra)
    try:
        _wait_for(lambda: _starts(box))
        pid = _starts(box)[0][1]
        _wait_for(lambda: (box / "data" / f"env.{pid}").exists())
        return dict(
            ln.split("=", 1)
            for ln in (box / "data" / f"env.{pid}").read_text().splitlines()
            if "=" in ln
        )
    finally:
        _stop(proc)


def test_agent_worker_address_is_passed_through(box: Path) -> None:
    """The address is an option (tests use a free port); the shape stays start_sh."""
    env = _child_env(box, "--agent-worker", "127.0.0.1:7123")
    assert env["VIGIL_MEDIC_AGENT_WORKER_ADDR"] == "127.0.0.1:7123"
    assert env["VIGIL_MEDIC_INSTALL_SHAPE"] == "start_sh"


@pytest.mark.parametrize(
    "addr", ["", "127.0.0.1", "http://127.0.0.1:6990", "127.0.0.1:69x0", "a b:1"]
)
def test_bad_agent_worker_address_exits_2(box: Path, addr: str) -> None:
    out = subprocess.run(
        _args(box, "--agent-worker", addr), capture_output=True, text=True, check=False
    )
    assert out.returncode == 2
    assert not _starts(box)


def test_term_stops_medic_and_does_not_restart(box: Path) -> None:
    (box / "data" / "mode").write_text("run")
    proc = _start(box, "--backoff-start", "0")
    _wait_for(lambda: _starts(box))
    assert _stop(proc) == 0
    child = _starts(box)[0][1]
    assert (box / "data" / "termed").read_text().split() == ["TERM", child]
    time.sleep(0.5)
    assert len(_starts(box)) == 1
    assert "stopped" in (box / "loop.log").read_text()


def test_exit_restarts_with_doubling_backoff(box: Path) -> None:
    (box / "data" / "mode").write_text("exit3")
    proc = _start(box, "--backoff-start", "1", "--backoff-max", "2", "--cap-exits", "9")
    try:
        _wait_for(lambda: len(_starts(box)) >= 4, timeout=20)
    finally:
        _stop(proc)
    times = [int(s[0]) for s in _starts(box)[:4]]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    # 1 s, then 2 s, then capped at 2 s (one-second clock, so allow ±1).
    assert 0 <= gaps[0] <= 2 and 1 <= gaps[1] <= 3 and 1 <= gaps[2] <= 3, gaps
    log = (box / "loop.log").read_text()
    assert "exited (code 3)" in log and "restarting in 1s" in log
    assert "restarting in 2s" in log


def test_term_during_backoff_stops_promptly(box: Path) -> None:
    (box / "data" / "mode").write_text("exit3")
    proc = _start(box, "--backoff-start", "60")
    _wait_for(lambda: "restarting in 60s" in (box / "loop.log").read_text())
    t0 = time.monotonic()
    assert _stop(proc) == 0
    assert time.monotonic() - t0 < 5
    assert len(_starts(box)) == 1


def test_crash_loop_cap_gives_up_and_marks_the_heartbeat(box: Path) -> None:
    (box / "data" / "mode").write_text("exit3")
    proc = _start(
        box, "--backoff-start", "0", "--cap-exits", "3", "--cap-window", "600"
    )
    assert proc.wait(timeout=20) == 1
    assert len(_starts(box)) == 4  # 3 exits allowed; the 4th gives up
    hb = box / "data" / "run" / "heartbeat"
    record = json.loads(hb.read_text())
    assert record["state"] == "crash-looping"
    assert hb.stat().st_mode & 0o777 == 0o600
    assert "crash-looping" in (box / "loop.log").read_text()


def test_bad_arguments_exit_2(box: Path) -> None:
    out = subprocess.run(
        ["bash", str(LOOP), "--nope"], capture_output=True, text=True, check=False
    )
    assert out.returncode == 2
    out = subprocess.run(
        ["bash", str(LOOP)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 2


# --- The real Medic under the loop ---------------------------------------------


def _check(box: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _args(box, "--check", python=sys.executable),
        capture_output=True,
        text=True,
        check=False,
    )


def _heartbeat_pid(box: Path) -> int | None:
    try:
        return json.loads((box / "data" / "run" / "heartbeat").read_text())["pid"]
    except (OSError, ValueError, KeyError):
        return None


def test_real_medic_runs_checks_green_and_restarts_after_kill(box: Path) -> None:
    """Flag on → Medic runs, `check` turns 0, kill -9 → restarted after the backoff."""
    (box / "app" / "services").symlink_to(REPO / "services")
    proc = _start(box, "--backoff-start", "1", python=sys.executable)
    try:
        _wait_for(lambda: _check(box).returncode == 0)
        first = _heartbeat_pid(box)
        assert first is not None
        os.kill(first, signal.SIGKILL)
        _wait_for(lambda: _heartbeat_pid(box) not in (None, first))
        _wait_for(lambda: _check(box).returncode == 0)
        log = (box / "loop.log").read_text()
        assert (
            f"exited (code {128 + signal.SIGKILL})" in log or "exited (code -9)" in log
        )
        assert "restarting in 1s" in log
    finally:
        assert _stop(proc) == 0
    # A clean stop leaves `check` red, so a stopped Medic never reads as healthy.
    assert _check(box).returncode != 0


class _ReadyStub:
    """The agent worker's /readyz on a free loopback port; counts the reads."""

    def __init__(self) -> None:
        import http.server
        import threading

        self.paths: list[str] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub.paths.append(self.path)
                body = b'{"ready": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addr = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_real_medic_reports_start_sh_and_reaches_the_worker(box: Path) -> None:
    """L49: under the loop, the real Medic runs as start_sh and reads /readyz."""
    (box / "app" / "services").symlink_to(REPO / "services")
    stub = _ReadyStub()
    proc = _start(box, "--agent-worker", stub.addr, python=sys.executable)
    try:
        _wait_for(lambda: "/readyz" in stub.paths, timeout=60)
        _wait_for(lambda: "start_sh" in (box / "loop.log").read_text())
    finally:
        assert _stop(proc) == 0
        stub.close()
    log = (box / "loop.log").read_text()
    assert "shape start_sh" in log, log
    assert "can't start" not in log
