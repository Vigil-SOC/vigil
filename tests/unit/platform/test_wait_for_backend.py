"""start.sh -d's backend gate (#2910): keep waiting while uvicorn lives, past
the old 60s; on failure stop it so a retry isn't refused as "already running".

Shell-function fakes of curl and sleep (which advances $SECONDS) make the 60s+
timelines instant; the "uvicorn" is a sleep renamed to a unique marker so the
pkill can't touch a real backend."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib.sh"


def _backend(marker: str) -> subprocess.Popen:
    return subprocess.Popen([f"uvicorn {marker}", "60"], executable="/bin/sleep")


def _gate(tmp_path: Path, pid: int, marker: str, timeout: int, healthy_after=""):
    pidfile, log = tmp_path / "backend.pid", tmp_path / "backend.log"
    pidfile.write_text(f"{pid}\n")
    log.write_text("Connecting MCP servers\n")
    script = (
        f'source "{LIB}"\n'
        # Health comes up on curl call $HEALTHY_AFTER (never if unset).
        "sleep() { SECONDS=$((SECONDS + $1)); }\n"
        'curl() { calls=$((${calls:-0} + 1)); [ -n "$HEALTHY_AFTER" ] && [ $calls -ge "$HEALTHY_AFTER" ]; }\n'
        "trap 'echo \"curl calls: ${calls:-0}\"' EXIT\n"
        f"if ! wait_for_backend http://127.0.0.1:1/api/health {pid} {timeout}; then\n"
        f'  abort_backend_start "{pidfile}" "{log}" "$MARKER"; exit 1\n'
        "fi\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        # Via env, not argv: pkill -f must not match this bash itself.
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HEALTHY_AFTER": str(healthy_after),
            "MARKER": marker,
        },
        timeout=60,
    )


def _alive(marker: str) -> bool:
    return (
        subprocess.run(
            ["pgrep", "-f", marker], check=False, capture_output=True
        ).returncode
        == 0
    )


def _assert_stopped(r, proc, marker, pidfile: Path):
    assert r.returncode == 1
    proc.wait(timeout=10)
    assert not _alive(marker)  # so start.sh's pgrep guard lets a retry through
    assert not pidfile.exists()
    assert "SOC daemon, LLM worker and agent layer were not started" in r.stderr
    assert "Connecting MCP servers" in r.stderr  # log tail


def test_health_after_60s_while_alive_succeeds(tmp_path):
    marker = f"vigil-test-{uuid.uuid4().hex}"
    proc = _backend(marker)
    try:
        r = _gate(tmp_path, proc.pid, marker, 300, healthy_after=70)
        assert r.returncode == 0, r.stderr
        assert "Backend startup still in progress (60s" in r.stdout
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_process_exit_fails_fast_and_cleans_up(tmp_path):
    marker = f"vigil-test-{uuid.uuid4().hex}"
    proc = subprocess.Popen(["true"])
    proc.wait()
    r = _gate(tmp_path, proc.pid, marker, 300)
    _assert_stopped(r, proc, marker, tmp_path / "backend.pid")
    assert "Backend exited during startup" in r.stderr
    assert "curl calls: 0" in r.stdout  # no health polling after death


def test_health_never_up_stops_backend_at_ceiling(tmp_path):
    marker = f"vigil-test-{uuid.uuid4().hex}"
    proc = _backend(marker)
    try:
        r = _gate(tmp_path, proc.pid, marker, 90)
        _assert_stopped(r, proc, marker, tmp_path / "backend.pid")
        assert "did not serve health within 90s" in r.stderr
    finally:
        proc.kill()
        proc.wait()
