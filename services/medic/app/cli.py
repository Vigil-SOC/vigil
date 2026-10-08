"""`python -m services.medic run | check`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.medic.app import config
from services.medic.app.gap import pending_gap, write_gap
from services.medic.app.heartbeat import (
    check_heartbeat,
    mark_stalled,
    write_heartbeat,
)
from services.medic.app.watchdog import Watchdog
from services.medic.app.wiring import (
    TICK_S,
    Medic,
    load_dev_rules,
    load_engine_state,
    save_engine_state,
)
from services.medic.redact import install_log_redaction
from services.medic.sensors import Sensor
from services.medic.sensors.http_ready import agent_worker_ready
from services.medic.store import StoreError, open_writer

log = logging.getLogger("services.medic")

USAGE = "usage: python -m services.medic {run|check}"

# httpcore's DEBUG trace logs raw response headers (Set-Cookie and the like) in a
# shape K2's patterns miss, and httpx logs every request at INFO. A sensor's
# target must never reach Medic's log, so these stay quiet below WARNING.
QUIET_LOGGERS = ("httpx", "httpcore")


@dataclass(frozen=True)
class SystemClock:
    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    clock: SystemClock | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
    watchdog_exit: Callable[[int], None] = os._exit,
    watchdog_poll_s: float = 10.0,
) -> int:
    """The test seams (`now`, `clock`, `sleep`, `max_cycles`, `watchdog_*`) default
    to real time and a real exit."""
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
    # Before anything logs (S3 → S4): every record this process writes is redacted.
    redaction = install_log_redaction()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        return run(
            env,
            data_dir,
            clock=clock or SystemClock(),
            sleep=sleep or asyncio.sleep,
            max_cycles=max_cycles,
            watchdog_exit=watchdog_exit,
            watchdog_poll_s=watchdog_poll_s,
        )
    finally:
        redaction.uninstall()  # only matters in-process (tests)


def run(
    env: Mapping[str, str],
    data_dir: Path,
    *,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    max_cycles: int | None,
    watchdog_exit: Callable[[int], None] = os._exit,
    watchdog_poll_s: float = 10.0,
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
    try:
        shape = config.install_shape(env)
        host, port = config.agent_worker_addr(env, shape)
    except config.ConfigError as exc:
        log.error("Medic can't start: %s", exc)
        return 1

    # C4 §6.1: everything Medic creates is private to its own uid. Restored on
    # return, which only matters when run() is called in-process (tests).
    old_umask = os.umask(0o077)
    try:
        return _run(
            data_dir,
            shape=shape,
            sensors=[agent_worker_ready(host=host, port=port)],
            clock=clock,
            sleep=sleep,
            max_cycles=max_cycles,
            watchdog_exit=watchdog_exit,
            watchdog_poll_s=watchdog_poll_s,
        )
    finally:
        os.umask(old_umask)


def _run(
    data_dir: Path,
    *,
    shape: str,
    sensors: Sequence[Sensor],
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    max_cycles: int | None,
    watchdog_exit: Callable[[int], None],
    watchdog_poll_s: float,
) -> int:
    started_at = clock.wall()
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.error("cannot write to the data dir %s: %s", data_dir, exc)
        return 1
    # Read before the new heartbeat replaces the last one (C5 §5.1, C4 §6.8).
    gap = pending_gap(data_dir, now=started_at)
    try:
        writer = open_writer(data_dir)
    except StoreError as exc:
        log.error("Medic can't open its store: %s", exc)
        return 1
    with writer:
        try:
            write_gap(writer, gap)
            write_heartbeat(
                data_dir,
                cycle=0,
                now=started_at,
                started_at=started_at,
                pid=os.getpid(),
            )
        except (OSError, StoreError, sqlite3.Error) as exc:
            log.error("cannot write to the data dir %s: %s", data_dir, exc)
            return 1
        medic = _build(writer, data_dir, shape, sensors, clock)
        log.info("Medic running; data dir %s, shape %s", data_dir, shape)
        watchdog = Watchdog(
            monotonic=clock.monotonic,
            exit_fn=watchdog_exit,
            poll_s=watchdog_poll_s,
            on_stall=lambda: mark_stalled(data_dir),
        )
        watchdog.start()
        try:
            cycle = asyncio.run(
                _loop(data_dir, medic, clock, sleep, watchdog, started_at, max_cycles)
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


def _build(writer, data_dir: Path, shape: str, sensors, clock) -> Medic:
    rules = load_dev_rules()
    state = load_engine_state(data_dir)
    try:
        return Medic(
            writer=writer,
            rules=rules,
            sensors=sensors,
            clock=clock,
            shape=shape,
            engine_state=state,
        )
    except (ValueError, KeyError, TypeError) as exc:
        if state is None:
            raise
        log.warning("Engine state not usable, starting fresh: %s", type(exc).__name__)
        return Medic(
            writer=writer, rules=rules, sensors=sensors, clock=clock, shape=shape
        )


async def _loop(
    data_dir: Path,
    medic: Medic,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    watchdog: Watchdog,
    started_at: float,
    max_cycles: int | None,
) -> int:
    """Run 15 s cycles until stopped; return the last completed cycle number."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    cycle = 0
    while not stop.is_set() and (max_cycles is None or cycle < max_cycles):
        # One cycle: sensors → K2 choke → engine tick → router → store.
        await medic.cycle()
        cycle += 1
        try:
            save_engine_state(data_dir, medic.engine.state())
            write_heartbeat(
                data_dir,
                cycle=cycle,
                now=clock.wall(),
                started_at=started_at,
                pid=os.getpid(),
            )
        except OSError as exc:
            # The loop is alive, so the watchdog keeps quiet; `check` goes stale and
            # the probe reports it. Disk-full handling belongs to the store (G3).
            log.error("cannot write the heartbeat: %s", exc)
        watchdog.beat()
        sleeper = asyncio.ensure_future(sleep(TICK_S))
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if sleeper.done():
            sleeper.result()  # a failing sleep is a failing loop
        else:
            sleeper.cancel()
    return cycle
