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

from services.medic.api.history import record_exit, record_start
from services.medic.api.ready import check_ready
from services.medic.api.server import Api, StatusBoard
from services.medic.api.status import build_status, read_chain
from services.medic.app import config
from services.medic.app.gap import pending_gap, write_gap
from services.medic.app.heartbeat import (
    check_heartbeat,
    mark_stalled,
    read_heartbeat,
    write_heartbeat,
)
from services.medic.app.policy_probe import Verdict
from services.medic.app.policy_probe import probe as policy_probe
from services.medic.app.watchdog import Watchdog
from services.medic.app.wiring import (
    TICK_S,
    Medic,
    load_dev_rules,
    load_dev_suppression,
    load_engine_state,
    save_engine_state,
)
from services.medic.redact import install_log_redaction
from services.medic.sensors import Sensor
from services.medic.sensors.http_ready import agent_serve_ready, agent_worker_ready
from services.medic.store import StoreError, open_writer

log = logging.getLogger("services.medic")

USAGE = "usage: python -m services.medic {run|check [--ready]}"

# httpcore's DEBUG trace logs raw response headers (Set-Cookie and the like) in a
# shape K2's patterns miss, and httpx logs every request at INFO. A sensor's
# target must never reach Medic's log, so these stay quiet below WARNING.
QUIET_LOGGERS = ("httpx", "httpcore")

# A3-3: the exit code for "this cluster doesn't enforce NetworkPolicy". Not 1, so
# an operator (and the kind CI) can tell a refusal from a crash.
POLICY_REFUSED = 3
# Medic usually starts before the gateway is Ready: the control is retried.
CONTROL_ATTEMPTS = 12
CONTROL_RETRY_S = 5.0
# A refused target counts only if it stays refused (S7-2): kube-proxy also
# rejects a Service with no endpoints yet. On Helm the target and the control
# share a Service (one EndpointSlice), so that can't happen there; the retries
# are a second guard for any other source of a passing refusal.
TARGET_ATTEMPTS = 3
TARGET_RETRY_S = 5.0
_probe_wait = time.sleep


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
    if list(argv) not in (["run"], ["check"], ["check", "--ready"]):
        print(USAGE, file=sys.stderr)
        return 2
    data_dir = config.data_dir(env, sys.platform)
    if list(argv) == ["check", "--ready"]:  # readiness: can the API serve (C5)
        ok, reason = check_ready(env, data_dir)
        print(reason)
        return 0 if ok else 1
    if argv[0] == "check":
        ok, reason = check_heartbeat(data_dir, now=time.time() if now is None else now)
        print(reason)
        return 0 if ok else 1
    # Before anything logs (S3 → S4): every record this process writes is redacted.
    redaction = install_log_redaction()
    levels = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
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
    finally:  # only matters in-process (tests)
        redaction.uninstall()
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


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
        sensors = sensors_for(env, shape)
        probe_target = config.policy_probe_addr(env, shape)
        probe_control = config.policy_control_addr(env, shape)
    except config.ConfigError as exc:
        log.error("Medic can't start: %s", exc)
        return 1
    # Before the store opens: a refusal writes nothing (no gap, no beat).
    if probe_target is not None and not _policy_enforced(probe_target, probe_control):
        return POLICY_REFUSED
    try:
        api = ApiSettings(
            config.api_bind(env),
            config.api_key_file(env, data_dir),
            config.api_bind_peer(env),
        )
    except config.ConfigError as exc:
        # The API is never a reason for Medic to stop (C5): watch without it.
        log.error("Medic's API is off: %s. Medic keeps watching.", exc)
        api = None

    # C4 §6.1: everything Medic creates is private to its own uid. Restored on
    # return, which only matters when run() is called in-process (tests).
    old_umask = os.umask(0o077)
    try:
        return _run(
            data_dir,
            shape=shape,
            sensors=sensors,
            clock=clock,
            sleep=sleep,
            max_cycles=max_cycles,
            watchdog_exit=watchdog_exit,
            watchdog_poll_s=watchdog_poll_s,
            api=api,
        )
    finally:
        os.umask(old_umask)


def sensors_for(env: Mapping[str, str], shape: str) -> list[Sensor]:
    """The sensors this shape runs; raises ConfigError on an unusable address."""
    host, port = config.agent_worker_addr(env, shape)
    sensors: list[Sensor] = [agent_worker_ready(host=host, port=port)]
    serve = config.agent_serve_addr(env, shape)
    if serve is not None:
        sensors.append(agent_serve_ready(host=serve[0], port=serve[1]))
    return sensors


def _policy_enforced(target: tuple[str, int], control: tuple[str, int]) -> bool:
    """A3-3. Proof = the allowed path connects AND the forbidden one is dropped
    or rejected.

    A timeout alone could be a dead pod network or a Service with no endpoints
    (IPVS drops those), so the control comes first (S7 review #1). On Helm the
    two are the gateway's two listeners on the same pods, so a connected control
    also proves the target has live endpoints. A refusal (S7-2: plugins that
    REJECT) must hold across TARGET_ATTEMPTS: endpoints that lag by a moment are
    refused once, then connect."""
    hint = (
        "Check that the Medic gateway is Running and Ready and that DNS works "
        "from Medic's pod."
    )
    for attempt in range(CONTROL_ATTEMPTS):
        checked = policy_probe(control)
        if checked is Verdict.CONNECTED:
            break
        if attempt < CONTROL_ATTEMPTS - 1:
            _probe_wait(CONTROL_RETRY_S)
    if checked is not Verdict.CONNECTED:
        log.error(
            "Medic refuses to run: it can't prove NetworkPolicy is enforced: the "
            "control connection to %s:%d, which its policy allows, was %s (A3-3). %s",
            *control,
            checked.value,
            hint,
        )
        return False
    for attempt in range(TARGET_ATTEMPTS):
        verdict = policy_probe(target)
        if verdict is not Verdict.REFUSED:
            break
        if attempt < TARGET_ATTEMPTS - 1:
            _probe_wait(TARGET_RETRY_S)
    if verdict.enforced:
        how = "dropped" if verdict is Verdict.BLOCKED else "refused every time"
        log.info("NetworkPolicy is enforced: the probe to %s:%d was %s", *target, how)
        return True
    if verdict is Verdict.CONNECTED:
        log.error(
            "Medic refuses to run: NetworkPolicy isn't enforced on this cluster. "
            "It reached %s:%d, which its own policy blocks, so nothing here would "
            "stop it reaching Vigil's backend or Redis (A3-3). Install a network "
            "plugin that enforces NetworkPolicy (Calico, Cilium, ...), or leave "
            "medic.enabled off.",
            *target,
        )
    else:
        log.error(
            "Medic refuses to run: it can't prove NetworkPolicy is enforced (%s "
            "for %s:%d; only a dropped or refused connection proves it, A3-3). %s",
            verdict.value,
            *target,
            hint,
        )
    return False


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
    api: ApiSettings | None = None,
) -> int:
    started_at = clock.wall()
    try:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.error("cannot write to the data dir %s: %s", data_dir, exc)
        return 1
    # Read before the new heartbeat replaces the last one (C5 §5.1, C4 §6.8).
    gap = pending_gap(data_dir, now=started_at)
    previous_beat = read_heartbeat(data_dir)
    try:
        writer = open_writer(data_dir)
    except StoreError as exc:
        log.error("Medic can't open its store: %s", exc)
        return 1
    with writer:
        # Built first: a start that can't build writes no gap and no beat, so a
        # crash loop neither grows the store nor reads healthy (S4 review #1).
        try:
            medic = _build(writer, data_dir, shape, sensors, clock)
        except Exception as exc:  # logged (redacted), exit 1
            log.exception("Medic can't start: %s", type(exc).__name__)
            return 1
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
        log.info("Medic running; data dir %s, shape %s", data_dir, shape)
        status = _Status(
            medic,
            data_dir,
            api,
            clock,
            started_at,
            record_start(data_dir, now=started_at, previous_beat=previous_beat),
        )
        status.publish(cycle=0)
        watchdog = Watchdog(
            monotonic=clock.monotonic,
            exit_fn=watchdog_exit,
            poll_s=watchdog_poll_s,
            on_stall=lambda: mark_stalled(data_dir),
        )
        watchdog.start()
        cycle, code = 0, 0
        try:
            cycle = asyncio.run(
                _loop(
                    data_dir,
                    medic,
                    clock,
                    sleep,
                    watchdog,
                    started_at,
                    max_cycles,
                    status.publish,
                )
            )
        # Logged here, through Medic's redaction, rather than as Python's own
        # unredacted traceback on stderr (S4 review #3). A kill is a BaseException
        # and isn't caught: nothing gets to say goodbye then.
        except Exception as exc:
            log.exception("Medic stopped on an error: %s", type(exc).__name__)
            code = 1
        finally:
            watchdog.stop()
            status.stop()
        record_exit(data_dir, now=clock.wall(), code=code)
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
    return code


def _build(writer, data_dir: Path, shape: str, sensors, clock) -> Medic:
    rules, suppression = load_dev_rules(), load_dev_suppression()
    state = load_engine_state(data_dir)
    last_tick = (state or {}).get("last_tick")
    if isinstance(last_tick, int | float) and last_tick > clock.wall() + TICK_S:
        # The clock stepped back: every tick would be skipped until it caught up.
        log.warning("Engine state is from the future, starting fresh")
        state = None
    try:
        return Medic(
            writer=writer,
            rules=rules,
            sensors=sensors,
            clock=clock,
            shape=shape,
            engine_state=state,
            suppression=suppression,
        )
    except Exception as exc:  # any bad state: start fresh, never a crash loop
        if state is None:
            raise
        log.warning("Engine state not usable, starting fresh: %s", type(exc).__name__)
        return Medic(
            writer=writer,
            rules=rules,
            sensors=sensors,
            clock=clock,
            shape=shape,
            suppression=suppression,
        )


async def _loop(
    data_dir: Path,
    medic: Medic,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    watchdog: Watchdog,
    started_at: float,
    max_cycles: int | None,
    after_cycle: Callable[..., None] = lambda **_: None,
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
        after_cycle(cycle=cycle)
        watchdog.beat()
        # To the next grid boundary, so a cycle's own run time never skips a tick.
        sleeper = asyncio.ensure_future(sleep(TICK_S - clock.wall() % TICK_S))
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if sleeper.done():
            sleeper.result()  # a failing sleep is a failing loop
        else:
            sleeper.cancel()
    return cycle


@dataclass(frozen=True)
class ApiSettings:
    bind: tuple[str, int]
    key_file: Path | None
    bind_peer: str | None = None


class _Status:
    """`/v1/status` (S9): a snapshot per cycle, published to the API's listener.

    Nothing here can stop the loop or fail `check` (C5): a snapshot that can't be
    built is skipped (the API answers `busy` once it's stale), and the listener
    retries on its own."""

    def __init__(self, medic, data_dir, api, clock, started_at, history) -> None:
        self.medic, self.data_dir, self.clock = medic, data_dir, clock
        self.started_at, self.history = started_at, history
        self.board = StatusBoard()
        self.api = None
        if api is not None:
            self.api = Api(
                api.bind,
                key_file=api.key_file,
                board=self.board,
                wall=clock.wall,
                monotonic=clock.monotonic,
                bind_peer=api.bind_peer,
            )
        self._failing = self._api_failed = False

    def publish(self, *, cycle: int) -> None:
        now = self.clock.wall()
        try:
            # The last beat actually written, not this cycle's attempt.
            beat = read_heartbeat(self.data_dir)
            snapshot = build_status(
                instance_id=self.medic.writer.instance_id,
                now=now,
                started_at=self.started_at,
                heartbeat_at=beat["ts"] if beat else self.started_at,
                cycle=cycle,
                history=self.history,
                scheduler=self.medic.scheduler.status(),
                usage=self.medic.writer.usage(),
                decisions_lost=self.medic.unstored,
                chain=read_chain(self.data_dir),
            )
        except Exception as exc:  # noqa: BLE001 (never the loop's problem)
            if not self._failing:
                log.error("Status snapshot not built: %s", type(exc).__name__)
            self._failing = True
        else:
            self._failing = False
            self.board.publish(snapshot, started_at=self.started_at, taken_at=now)
        if self.api is not None:
            try:
                self.api.ensure()
            except Exception as exc:  # noqa: BLE001 (never the loop's problem)
                if not self._api_failed:
                    log.error("Medic's API failed: %s", type(exc).__name__)
                self._api_failed = True

    def stop(self) -> None:
        if self.api is not None:
            self.api.stop()
