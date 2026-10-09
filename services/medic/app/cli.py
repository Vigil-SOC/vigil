"""`python -m services.medic run | check`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.medic.app import config
from services.medic.app.heartbeat import (
    BEAT_INTERVAL_S,
    check_heartbeat,
    write_heartbeat,
)
from services.medic.app.watchdog import Watchdog

log = logging.getLogger("services.medic")

USAGE = "usage: python -m services.medic {run|check}"


@dataclass(frozen=True)
class SystemClock:
    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    clock: SystemClock | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> int:
    """The test seams (`now`, `clock`, `sleep`, `max_cycles`) default to real time."""
    # Medic reads its own env: it may not import core.config (A3-1).
    env = os.environ if env is None else env  # noqa: ENV001
    if len(argv) != 1 or argv[0] not in ("run", "check"):
        print(USAGE, file=sys.stderr)
        return 2
    data_dir = config.data_dir(env, sys.platform)
    if argv[0] == "check":
        ok, reason = check_heartbeat(data_dir, now=time.time() if now is None else now)
        print(reason)
        return 0 if ok else 1
    return run(
        env,
        data_dir,
        clock=clock or SystemClock(),
        sleep=sleep or asyncio.sleep,
        max_cycles=max_cycles,
    )


def run(
    env: Mapping[str, str],
    data_dir: Path,
    *,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    max_cycles: int | None,
) -> int:
    if not config.is_enabled(env):
        value = config.flag_value(env)
        shown = "unset" if value is None else repr(value)
        hint = "" if config.is_recognised(value) else " (not a recognised value)"
        # Exit 0 (decided S1-1): off is a choice, not a failure.
        log.warning(
            "Medic is off: %s is %s%s. Set %s=true to run it. Exiting.",
            config.ENABLED_VAR,
            shown,
            hint,
            config.ENABLED_VAR,
        )
        return 0

    # C4 §6.1: everything Medic creates is private to its own uid. Restored on
    # return, which only matters when run() is called in-process (tests).
    old_umask = os.umask(0o077)
    try:
        return _run(data_dir, clock=clock, sleep=sleep, max_cycles=max_cycles)
    finally:
        os.umask(old_umask)


def _run(
    data_dir: Path,
    *,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    max_cycles: int | None,
) -> int:
    started_at = clock.wall()
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_heartbeat(
            data_dir, cycle=0, now=started_at, started_at=started_at, pid=os.getpid()
        )
    except OSError as exc:
        log.error("cannot write to the data dir %s: %s", data_dir, exc)
        return 1

    log.info("Medic running; data dir %s", data_dir)
    watchdog = Watchdog(monotonic=clock.monotonic)
    watchdog.start()
    try:
        cycle = asyncio.run(
            _loop(data_dir, clock, sleep, watchdog, started_at, max_cycles)
        )
    finally:
        watchdog.stop()
    # So `check` doesn't call a stopped Medic healthy for the next 120 s.
    try:
        write_heartbeat(
            data_dir,
            cycle=cycle,
            now=clock.wall(),
            started_at=started_at,
            pid=os.getpid(),
            state="stopped",
        )
    except OSError as exc:
        log.error("cannot write the heartbeat: %s", exc)
    log.info("Medic stopped")
    return 0


async def _loop(
    data_dir: Path,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    watchdog: Watchdog,
    started_at: float,
    max_cycles: int | None,
) -> int:
    """Run cycles until stopped; return the last completed cycle number."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    cycle = 0
    while not stop.is_set() and (max_cycles is None or cycle < max_cycles):
        # One cycle. Sensors and the engine plug in here (S4); for now it only idles.
        sleeper = asyncio.ensure_future(sleep(BEAT_INTERVAL_S))
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        sleeper.cancel()
        stopper.cancel()
        if stop.is_set():
            break
        cycle += 1
        try:
            write_heartbeat(
                data_dir,
                cycle=cycle,
                now=clock.wall(),
                started_at=started_at,
                pid=os.getpid(),
            )
        except OSError as exc:
            # The loop is alive, so the watchdog keeps quiet; `check` goes stale and
            # the probe reports it. Disk-full handling belongs to the store (S2).
            log.error("cannot write the heartbeat: %s", exc)
        watchdog.beat()
    return cycle
