"""Test-only (compose.eval.yml mounts it into soc-daemon; never for installs).

Python imports this at start. Inert until SIGUSR1 arrives, then it cancels the
daemon's `scheduler` task, which /health then reports as "stopped" (P-3.a). The
daemon has no outside lever for that. No Vigil imports.
"""

import asyncio
import signal


def _cancel_scheduler(signum, frame):
    loop = asyncio.get_running_loop()  # the handler runs on the main thread, in the loop
    for task in asyncio.all_tasks(loop):
        if task.get_name() == "scheduler":
            loop.call_soon_threadsafe(task.cancel)


signal.signal(signal.SIGUSR1, _cancel_scheduler)
