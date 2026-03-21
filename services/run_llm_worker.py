"""Launcher for the ARQ LLM worker(s).

Python 3.12+ removed implicit event loop creation in the main thread.
This wrapper ensures an event loop exists before ARQ's Worker initialises.

Behaviour
---------
* If ``LLM_WORKER_QUEUE`` is **not** set (the default), all four priority
  queues are started as separate sub-processes in the same process group so
  that triage, investigation, chat, and insights jobs are all drained::

      python -m services.run_llm_worker          # starts all 4 workers

* If ``LLM_WORKER_QUEUE`` is set to a specific queue name, only that single
  worker is started (useful for scaling individual tiers independently)::

      LLM_WORKER_QUEUE=arq:llm:triage python -m services.run_llm_worker
"""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.llm_gateway import (
    QUEUE_CHAT,
    QUEUE_INSIGHTS,
    QUEUE_INVESTIGATION,
    QUEUE_TRIAGE,
)

ALL_QUEUES = [QUEUE_TRIAGE, QUEUE_INVESTIGATION, QUEUE_CHAT, QUEUE_INSIGHTS]


def _run_single_worker():
    """Start a single ARQ worker for the currently configured queue."""
    from arq.worker import run_worker
    from services.llm_worker import WorkerSettings

    asyncio.set_event_loop(asyncio.new_event_loop())
    run_worker(WorkerSettings)


def _run_all_workers():
    """Spawn one sub-process per priority queue and wait for them all.

    Propagates SIGINT/SIGTERM to every child so Ctrl-C shuts everything down
    cleanly.
    """
    procs: list[subprocess.Popen] = []
    python = sys.executable

    for queue in ALL_QUEUES:
        env = os.environ.copy()
        env["LLM_WORKER_QUEUE"] = queue
        proc = subprocess.Popen(
            [python, "-m", "services.run_llm_worker"],
            env=env,
        )
        print(f"Started LLM worker for queue '{queue}' (PID {proc.pid})", flush=True)
        procs.append(proc)

    def _stop_all(signum, frame):
        for proc in procs:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    for proc in procs:
        proc.wait()


def main():
    if os.environ.get("LLM_WORKER_QUEUE"):
        # A specific queue was requested — run that single worker in-process.
        _run_single_worker()
    else:
        # Default: start all four priority-queue workers so every job type
        # is processed even in a minimal single-command dev startup.
        _run_all_workers()


if __name__ == "__main__":
    main()
