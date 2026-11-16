"""Test-only (compose.eval.yml mounts it into soc-daemon; never for installs).

Python imports this at start. Inert until SIGUSR1, then the scheduler task
raises RuntimeError, so /health reads "failed: RuntimeError" (P-3.a). Cancelling
the task would read "stopped". No Vigil imports.
"""

import asyncio
import builtins
import signal

_fail = False


def _arm(signum, frame):
    global _fail
    _fail = True


def _wrap(mod) -> None:
    orig = mod.TaskScheduler.run

    async def run(self, shutdown_event):
        inner = asyncio.ensure_future(orig(self, shutdown_event))
        try:
            while not inner.done():
                if _fail:
                    raise RuntimeError("eval")
                await asyncio.wait({inner}, timeout=0.2)
            return inner.result()
        finally:
            if not inner.done():
                inner.cancel()

    mod.TaskScheduler.run = run
    mod._eval_wrapped = True


_real_import = builtins.__import__


def _import(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _real_import(name, globals, locals, fromlist, level)
    if name == "services.daemon.scheduler" and not getattr(mod, "_eval_wrapped", False):
        _wrap(mod)
    return mod


builtins.__import__ = _import
signal.signal(signal.SIGUSR1, _arm)
