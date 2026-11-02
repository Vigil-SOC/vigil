"""wait_for_backend in scripts/lib.sh times out on elapsed time, not the wall clock."""

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib.sh"

pytestmark = pytest.mark.skipif(
    not os.path.exists("/proc/uptime"), reason="macOS falls back to $SECONDS"
)


def _wait(curl_stub: str, timeout: int) -> subprocess.CompletedProcess:
    script = f"""
source "{LIB}"
sleep 30 >/dev/null 2>&1 &
pid=$!
calls=0
curl() {{ calls=$((calls + 1)); {curl_stub}; }}
rc=0
wait_for_backend http://unused "$pid" {timeout} || rc=$?
kill "$pid"
echo "calls=$calls rc=$rc"
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def test_a_wall_clock_jump_does_not_end_the_wait():
    # Assigning SECONDS moves it the way a stepped clock does.
    done = _wait('SECONDS=100000; [ "$calls" -ge 3 ]', timeout=300)
    assert "calls=3 rc=0" in done.stdout, done.stdout + done.stderr


def test_time_blocked_in_curl_counts_against_the_timeout():
    done = _wait("sleep 2; return 1", timeout=2)
    assert "calls=1 rc=1" in done.stdout, done.stdout + done.stderr
