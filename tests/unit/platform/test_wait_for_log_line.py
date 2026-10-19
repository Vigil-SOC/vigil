"""wait_for_log_line in scripts/lib.sh: start.sh's readiness gate for the
daemon and LLM worker (#2321). A process that dies at startup must fail the
wait, not slip through to "Ready"."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib.sh"


def _wait(tmp_path: Path, child: str, timeout: int = 10):
    log = tmp_path / "w.log"
    script = (
        f'source "{LIB}"\n'
        f'( {child} ) > "{log}" 2>&1 &\n'
        f'wait_for_log_line "Worker" $! "{log}" "worker started" {timeout}\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_ready_once_line_logged_and_process_alive(tmp_path):
    r = _wait(tmp_path, "sleep 1; echo 'worker started'; sleep 30")
    assert r.returncode == 0, r.stderr


def test_process_that_dies_fails_fast_with_log_tail(tmp_path):
    r = _wait(tmp_path, "echo 'TimeoutError: connecting'; exit 1")
    assert r.returncode == 1
    assert "Worker exited during startup" in r.stderr
    assert "TimeoutError: connecting" in r.stderr


def test_silent_process_times_out(tmp_path):
    r = _wait(tmp_path, "sleep 30", timeout=2)
    assert r.returncode == 1
    assert "did not report ready within 2s" in r.stderr
