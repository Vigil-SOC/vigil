"""The host-native fault cells (SW I2, lane 1): P-1.b and P-3.c.

Vigil here is processes on the host, not a Compose project, so these cells run on
their own small stand-in for `Stack` (HostStack). Both are an orphan holding a
port Vigil's own process needs. scripts/agent_up.sh and shutdown_all.sh free
those ports only through pidfiles, never by killing whatever holds the port, so
an untracked listener stays until someone finds it.

- P-1.b: a listener on 127.0.0.1:6989 and :6990 (agent-serve's and the agent
  worker's ports), standing in for an agent process no pidfile tracks.
- P-3.c: a stale listener answering /health on the daemon's health port,
  ${DAEMON_HEALTH_PORT:-9091} as scripts/medic/host-native/medic.sh passes it.

verify reads the host (who listens on the port), never Medic. revert kills only
the PID inject started, never whatever holds the port.

`detect_within_s` is 300: no merged rule names either cause yet (the same as
config_cells.py's cells whose rule is still a draft).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from tests.medic_eval.inject.contract import Cell

AGENT_PORTS = (6989, 6990)

# A listener that answers every GET with 200, the way a stale server would.
LISTENER = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def listener_pids(port: int) -> set[int]:
    """PIDs listening on a TCP port: ss on Linux, lsof otherwise, as shutdown_all.sh does."""
    if shutil.which("ss"):
        out = subprocess.run(
            ["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True
        ).stdout
        return {int(p) for p in re.findall(r"pid=(\d+)", out)}
    out = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
    ).stdout
    return {int(p) for p in out.split()}


class HostStack:
    """The listeners this run started, by port. Nothing else is ever signalled."""

    def __init__(self) -> None:
        self.procs: dict[int, subprocess.Popen] = {}

    def start(self, port: int) -> None:
        p = self.procs.get(port)
        if p is not None and p.poll() is None:
            return
        p = self.procs[port] = subprocess.Popen(
            [sys.executable, "-c", LISTENER, str(port)], stderr=subprocess.PIPE
        )
        deadline = time.monotonic() + 15
        while p.pid not in listener_pids(port):
            if p.poll() is not None:  # it could not bind: something else holds the port
                err = p.stderr.read().decode().strip().splitlines()[-1:]
                raise AssertionError(f"port {port} is not free: {err}")
            assert time.monotonic() < deadline, f"no listener on {port} after 15 s"
            time.sleep(0.2)

    def stop(self, port: int) -> None:
        p = self.procs.get(port)
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        p.stderr.close()

    def holds(self, port: int) -> bool:
        """True while a listener this run started is the one on the port."""
        p = self.procs.get(port)
        return p is not None and p.pid in listener_pids(port)


def _cell(cause: str, ports, what: str) -> Cell:
    def inject(s: HostStack) -> None:
        for port in ports():
            s.start(port)

    def revert(s: HostStack) -> None:
        for port in ports():
            s.stop(port)

    def verify(s: HostStack, faulted: bool) -> None:
        for port in ports():
            if faulted:
                assert s.holds(port), f"no injected listener on 127.0.0.1:{port}"
                with socket.create_connection(("127.0.0.1", port), timeout=5):
                    pass
                got = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
                assert got.status == 200, f"GET :{port}/health: {got.status}"
            else:
                assert not s.holds(port), f"injected listener still on :{port}"

    where = ", ".join(f"127.0.0.1:{p}" for p in ports())
    return Cell(
        cause,
        1,
        300,  # no merged rule names this cause yet
        inject,
        verify,
        revert,
        (f"start an untracked {what} listener on {where}",),
        (f"kill the listener's own PID on {where}",),
    )


CELLS = (
    _cell("P-1.b", lambda: AGENT_PORTS, "orphaned agent"),
    _cell(
        "P-3.c",
        lambda: (int(os.environ.get("DAEMON_HEALTH_PORT") or 9091),),
        "stale daemon /health",
    ),
)
