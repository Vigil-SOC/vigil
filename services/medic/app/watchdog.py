"""In-process watchdog (C5 §5.1): turns a hung main loop into an exit.

Compose and host-native restart a process only when it exits, so a loop that
hangs with the process still alive (C5 W-2) would otherwise never recover. A
frozen process (W-3) stops this thread too; only the outside view catches that.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable

from services.medic.app.heartbeat import WATCHDOG_AFTER_S

log = logging.getLogger("services.medic")

# EX_SOFTWARE: non-zero, so restart-on-failure policies restart it.
STALL_EXIT_CODE = 70


class Watchdog:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        exit_fn: Callable[[int], None] = os._exit,
        after_s: float = WATCHDOG_AFTER_S,
        poll_s: float = 10.0,
        on_stall: Callable[[], None] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._exit = exit_fn
        self._on_stall = on_stall
        self._after_s = after_s
        self._poll_s = poll_s
        self._last = monotonic()
        self._main_ident = threading.main_thread().ident
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """Called by the main loop after each completed cycle."""
        self._last = self._monotonic()

    def check(self) -> bool:
        """Exit the process if the loop has stalled. Returns True if it fired."""
        stalled_for = self._monotonic() - self._last
        if stalled_for <= self._after_s:
            return False
        frame = sys._current_frames().get(self._main_ident)
        stack = (
            "".join(traceback.format_stack(frame)) if frame else "(main thread gone)"
        )
        log.error(
            "main loop stalled for %.0f s (limit %.0f s); exiting. Stack:\n%s",
            stalled_for,
            self._after_s,
            stack,
        )
        # C5 §5.1 step 2: say why, so the next start records a `stalled` gap. The
        # store isn't touched from here: the hung loop may hold its writer.
        if self._on_stall is not None:
            try:
                self._on_stall()
            except Exception as exc:  # noqa: BLE001 (exit whatever happens)
                log.error("could not mark the stall: %s", type(exc).__name__)
        self._exit(STALL_EXIT_CODE)
        return True

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._watch, name="medic-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _watch(self) -> None:
        while not self._stop.wait(self._poll_s):
            if self.check():
                return
