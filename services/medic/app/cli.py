"""`python -m services.medic run | check`."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from services.medic.api.history import record_exit, record_start
from services.medic.api.ready import check_ready
from services.medic.api.sensors import build_sensors
from services.medic.api.server import Api, StatusBoard
from services.medic.api.status import build_status, read_chain
from services.medic.app import config, policy_refusal
from services.medic.app.gap import pending_gap, write_gap
from services.medic.app.heartbeat import (
    check_heartbeat,
    mark_stalled,
    read_heartbeat,
    write_heartbeat,
)
from services.medic.app.pack_admin import PackAdmin
from services.medic.app.policy_probe import Policy, PolicyCheck, Verdict
from services.medic.app.policy_probe import probe as policy_probe
from services.medic.app.trace import TraceCapture, pack_of, start_capture
from services.medic.app.watchdog import Watchdog
from services.medic.app.wiring import (
    TICK_S,
    Loaded,
    Medic,
    PolicySensor,
    PolicyStartupSensor,
    RecordingSensor,
    StoreResetSensor,
    grid_tick,
    load_engine_state,
    load_pack,
    rebuild_machines,
    save_engine_state,
    store_head,
)
from services.medic.engine.core import iso
from services.medic.redact import install_log_redaction
from services.medic.sensors import Sensor
from services.medic.sensors.containers import docker_containers
from services.medic.sensors.daemon import backend_status_sensors, daemon_sensors
from services.medic.sensors.federation import federation_sources
from services.medic.sensors.gateway import address as gateway_address
from services.medic.sensors.gateway import gateway_session
from services.medic.sensors.http_ready import agent_serve_ready, agent_worker_ready
from services.medic.sensors.kube import agent_worker_pods, kube_pods
from services.medic.sensors.logtail import compose_log_sensors, helm_log_sensors
from services.medic.sensors.providers import llm_providers
from services.medic.sensors.work import work_sensors
from services.medic.store import RecordHeld, StoreError, open_writer

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
        # Validates the addresses now; the sensors are built again once the
        # pack's catalogue is known (it needs the data dir, opened later).
        sensors_for(env, shape, ())
        probe_target = config.policy_probe_addr(env, shape)
        probe_control = config.policy_control_addr(env, shape)
        policy_check = (
            None
            if probe_target is None
            else functools.partial(_policy_state, probe_target, probe_control)
        )
        store_cap = config.store_max_bytes(env)
    except config.ConfigError as exc:
        log.error("Medic can't start: %s", exc)
        return 1
    # Before the store opens: a refusal writes no gap and no beat, only its marker.
    refused_at = None
    if probe_target is not None:
        if not _policy_enforced(probe_target, probe_control, data_dir, clock.wall()):
            return POLICY_REFUSED
        refused_at = policy_refusal.read(data_dir)
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
            sensors=functools.partial(sensors_for, env, shape),
            clock=clock,
            sleep=sleep,
            max_cycles=max_cycles,
            watchdog_exit=watchdog_exit,
            watchdog_poll_s=watchdog_poll_s,
            api=api,
            vigil_dev_mode=config.dev_mode(env),
            store_cap=store_cap,
            policy_check=policy_check,
            policy_refused_at=refused_at,
            trace_id=config.trace_id(env),
        )
    finally:
        os.umask(old_umask)


def sensors_for(
    env: Mapping[str, str], shape: str, catalog: Collection[tuple[str, str]]
) -> list[Sensor]:
    """The sensors this shape runs; raises ConfigError on an unusable address.

    Helm reads each agent-worker pod and follows pod logs. Compose and
    host-native read one address.
    """
    if shape == "helm":
        sensors: list[Sensor] = [agent_worker_pods()]
    else:
        host, port = config.agent_worker_addr(env, shape)
        sensors = [agent_worker_ready(host=host, port=port)]
    serve = config.agent_serve_addr(env, shape)
    if serve is not None:
        sensors.append(agent_serve_ready(host=serve[0], port=serve[1]))
    # Compose reads containers through the proxy. Helm reads pods from the API.
    if shape == "compose":
        proxy = config.dockerproxy_addr(env, shape)
        if proxy is not None:
            sensors.append(docker_containers(host=proxy[0], port=proxy[1]))
            sensors.extend(compose_log_sensors(proxy[0], proxy[1], catalog=catalog))
    if shape == "helm":
        sensors.append(kube_pods())
        sensors.extend(helm_log_sensors(catalog=catalog))
    daemon = config.daemon_addr(env, shape)
    if daemon is not None:
        sensors.extend(daemon_sensors(host=daemon[0], port=daemon[1]))
    gateway = gateway_address(env, shape)
    if gateway is not None:
        sensors.append(gateway_session(host=gateway[0], port=gateway[1]))
        sensors.append(federation_sources(host=gateway[0], port=gateway[1]))
        sensors.append(llm_providers(host=gateway[0], port=gateway[1]))
        sensors.extend(work_sensors(host=gateway[0], port=gateway[1]))
        sensors.extend(backend_status_sensors(host=gateway[0], port=gateway[1]))
    return sensors


def _policy_state(target: tuple[str, int], control: tuple[str, int]) -> PolicyCheck:
    """A3-3. Proof = the allowed path connects AND the forbidden one is dropped
    or rejected. Only a connection to the forbidden target is UNENFORCED; a
    failed control or an inconclusive probe is UNKNOWN. Blocks: run it in a thread
    when the event loop is up. The caller decides what to log and whether to exit.

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
        return PolicyCheck(
            Policy.UNKNOWN,
            "it can't prove NetworkPolicy is enforced: the control connection to "
            f"{control[0]}:{control[1]}, which its policy allows, was "
            f"{checked.value} (A3-3). {hint}",
        )
    for attempt in range(TARGET_ATTEMPTS):
        verdict = policy_probe(target)
        if verdict is not Verdict.REFUSED:
            break
        if attempt < TARGET_ATTEMPTS - 1:
            _probe_wait(TARGET_RETRY_S)
    where = f"{target[0]}:{target[1]}"
    if verdict.enforced:
        how = "dropped" if verdict is Verdict.BLOCKED else "refused every time"
        return PolicyCheck(
            Policy.ENFORCED,
            f"NetworkPolicy is enforced: the probe to {where} was {how}",
        )
    if verdict is Verdict.CONNECTED:
        return PolicyCheck(
            Policy.UNENFORCED,
            "NetworkPolicy isn't enforced on this cluster. "
            f"It reached {where}, which its own policy blocks, so nothing here "
            "would stop it reaching Vigil's backend or Redis (A3-3). Install a "
            "network plugin that enforces NetworkPolicy (Calico, Cilium, ...), "
            "or leave medic.enabled off.",
        )
    return PolicyCheck(
        Policy.UNKNOWN,
        f"it can't prove NetworkPolicy is enforced ({verdict.value} for {where}; "
        f"only a dropped or refused connection proves it, A3-3). {hint}",
    )


def _policy_enforced(
    target: tuple[str, int], control: tuple[str, int], data_dir: Path, now: float
) -> bool:
    """The startup gate: False refuses the run, and leaves the marker a later
    clean start reads."""
    found = _policy_state(target, control)
    if found.state is Policy.ENFORCED:
        log.info("%s", found.text)
        return True
    log.error("Medic refuses to run: %s", found.text)
    try:
        policy_refusal.record(data_dir, now)
    except OSError as exc:
        log.warning("Medic couldn't note the refusal in %s: %s", data_dir, exc)
    return False


def _run(
    data_dir: Path,
    *,
    shape: str,
    sensors: Callable[[Collection[tuple[str, str]]], list[Sensor]],
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    max_cycles: int | None,
    watchdog_exit: Callable[[int], None],
    watchdog_poll_s: float,
    api: ApiSettings | None = None,
    vigil_dev_mode: bool = False,
    store_cap: int = config.STORE_MAX_MB_DEFAULT * 2**20,
    policy_check: Callable[[], PolicyCheck] | None = None,
    policy_refused_at: float | None = None,
    trace_id: str | None = None,
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
        writer = open_writer(data_dir, cap_bytes=store_cap, shape=shape)
    except StoreError as exc:
        log.error("Medic can't open its store: %s", exc)
        return 1
    # The store is open: a refusal that lasted opens one incident (true, then
    # false a tick later); a short one is forgotten.
    policy_sensors: list[Sensor] = []
    if policy_check is not None:
        pending = (
            policy_refused_at is not None
            and started_at - policy_refused_at >= policy_refusal.MIN_AGE_S
        )
        policy_refusal.clear(data_dir)
        if pending:
            policy_sensors.append(PolicyStartupSensor())
        policy_sensors.append(PolicySensor(policy_check, skip_first=pending))
    with writer:
        # Built first: a start that can't build writes no gap and no beat, so a
        # crash loop neither grows the store nor reads healthy (S4 review #1).
        try:
            # The lifecycle needs the data dir, so the pack loads here, and its
            # catalogue goes to the sensors (the log tails read it).
            pack = load_pack(
                shape,
                dev_mode=vigil_dev_mode,
                data_dir=data_dir,
                now=datetime.fromtimestamp(started_at, UTC),
            )
            medic = _build(
                writer,
                data_dir,
                shape,
                pack,
                [
                    *sensors(pack.catalog),
                    *policy_sensors,
                    RecordingSensor(writer),
                    StoreResetSensor(writer),
                ],
                clock,
                vigil_dev_mode=vigil_dev_mode,
                capture=start_capture(
                    data_dir,
                    trace_id,
                    shape=shape,
                    instance_id=writer.instance_id,
                    tick=grid_tick(clock),
                    pack=pack_of(pack.rules),
                ),
            )
        except config.ConfigError as exc:
            log.error("Medic can't start: %s", exc)
            return 1
        except Exception as exc:  # logged (redacted), exit 1; a bad dev rule too
            log.exception("Medic can't start: %s", type(exc).__name__)
            return 1
        try:
            try:
                if stored := write_gap(writer, gap):
                    medic.head_seq = stored["seq"]
            except RecordHeld:  # stopped at start: written, in order, on resume
                medic.held.append({"v": 1, "at": gap["to"], "type": "gap", "body": gap})
            for event in pack.events:  # what the pack's start-up did, after the gap
                medic._store(
                    {"v": 1, "at": iso(started_at), "type": "pack_event", "body": event}
                )
            write_heartbeat(
                data_dir,
                cycle=0,
                now=started_at,
                started_at=started_at,
                pid=os.getpid(),
                state="degraded" if writer.stopped else "running",
            )
        except (OSError, StoreError, sqlite3.Error) as exc:
            log.error("cannot write to the data dir %s: %s", data_dir, exc)
            return 1
        log.info("Medic running; data dir %s, shape %s", data_dir, shape)
        # Dev mode, and a start whose lifecycle failed, have none.
        admin = (
            PackAdmin(pack, shape=shape, data_dir=data_dir, clock=clock)
            if pack.context is not None
            else None
        )
        status = _Status(
            medic,
            data_dir,
            api,
            clock,
            started_at,
            record_start(data_dir, now=started_at, previous_beat=previous_beat),
            dev_mode=vigil_dev_mode,
            admin=admin,
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
                    admin,
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
            medic.capture.close(clock.wall())  # the manifest, on a clean stop
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


def _build(
    writer,
    data_dir: Path,
    shape: str,
    pack: Loaded,
    sensors,
    clock,
    *,
    vigil_dev_mode: bool = False,
    capture: TraceCapture | None = None,
) -> Medic:
    rules, suppression = pack.rules, pack.suppression
    state = load_engine_state(data_dir)
    last_tick = (state or {}).get("last_tick")
    if isinstance(last_tick, int | float) and last_tick > clock.wall() + TICK_S:
        # The clock stepped back: every tick would be skipped until it caught up.
        log.warning("Engine state is from the future, starting fresh")
        state = None

    def medic_from(state: dict | None, why: str | None = None) -> Medic:
        saved = (state or {}).get("head_seq")
        head = -1 if saved is None else saved
        try:
            head = store_head(data_dir)
            if why is None and state is None:
                why = "the engine state is missing or unusable"
            elif why is None and saved is None:
                why = "the engine state has no store head"
            elif why is None and saved != head:
                why = f"the store is at seq {head}, the state at {saved}"
        except (StoreError, sqlite3.Error) as exc:  # the saved state still stands
            log.warning("Can't read the store's head: %s", type(exc).__name__)
            why = None
        if why is not None:
            try:  # the store is the truth; the engine state is a cache
                state, resumed = rebuild_machines(data_dir, state, rules)
                log.log(
                    logging.WARNING if resumed else logging.INFO,
                    "Rebuilt the open incidents from the store (%s): %d resumed",
                    why,
                    resumed,
                )
            except (StoreError, sqlite3.Error, KeyError, ValueError) as exc:
                log.warning("Can't rebuild from the store: %s", type(exc).__name__)
        medic = Medic(
            writer=writer,
            rules=rules,
            sensors=sensors,
            clock=clock,
            shape=shape,
            engine_state=state,
            suppression=suppression,
            vigil_dev_mode=vigil_dev_mode,
            capture=capture,
            runbooks=pack.runbooks,
        )
        medic.head_seq = head
        return medic

    try:
        return medic_from(state, None)
    except Exception as exc:  # any bad state: start fresh, never a crash loop
        if state is None:
            raise
        log.warning("Engine state not usable, starting fresh: %s", type(exc).__name__)
        return medic_from(None, "the engine state is not usable")


async def _loop(
    data_dir: Path,
    medic: Medic,
    clock: SystemClock,
    sleep: Callable[[float], Awaitable[None]],
    watchdog: Watchdog,
    started_at: float,
    max_cycles: int | None,
    after_cycle: Callable[..., None] = lambda **_: None,
    admin: PackAdmin | None = None,
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
            save_engine_state(data_dir, medic.state())
            write_heartbeat(
                data_dir,
                cycle=cycle,
                now=clock.wall(),
                started_at=started_at,
                pid=os.getpid(),
                # `check` still passes (degraded is healthy): a pod out of disk
                # isn't helped by a restart.
                state="degraded" if medic.writer.stopped else "running",
            )
        except OSError as exc:
            # The loop is alive, so the watchdog keeps quiet; `check` goes stale and
            # the probe reports it.
            log.error("cannot write the heartbeat: %s", exc)
        if admin is not None:  # the cycle boundary: the one place the pack swaps
            admin.apply_pending(medic)
        try:
            after_cycle(cycle=cycle)
        finally:
            if admin is not None:  # only now: the snapshot already shows the swap
                admin.release()
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

    def __init__(
        self, medic, data_dir, api, clock, started_at, history, *, dev_mode, admin
    ) -> None:
        self.medic, self.data_dir, self.clock = medic, data_dir, clock
        self.dev_mode, self.admin = dev_mode, admin
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
                data_dir=data_dir,
                writer=medic.writer,
                bridge=admin.bridge if admin else None,
                bind_peer=api.bind_peer,
            )
        self._failing = self._api_failed = self._pack_failing = False

    def _pack_state(self, now: float) -> dict | None:
        """The `PackState` for this snapshot; None (so `busy`) if it can't be built."""
        if self.admin is None:
            return None
        try:
            state = self.admin.state(datetime.fromtimestamp(now, UTC))
            self._pack_failing = False
            return state
        except Exception as exc:  # noqa: BLE001 (never the loop's problem)
            if not self._pack_failing:
                log.error("Pack state not built: %s", type(exc).__name__)
            self._pack_failing = True
            return None

    def publish(self, *, cycle: int) -> None:
        now = self.clock.wall()
        pack_state = self._pack_state(now)
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
                trace=self.medic.capture.status(),
                dev_mode=self.dev_mode,
                pack=self.admin.facts if self.admin else None,
                upgrade_hold_until=self.medic.engine.upgrade.end,
            )
            sensors = build_sensors(
                self.medic.scheduler.sensor_states(),
                self.medic.engine.status(),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 (never the loop's problem)
            if not self._failing:
                log.error("Status snapshot not built: %s", type(exc).__name__)
            self._failing = True
        else:
            self._failing = False
            self.board.publish(
                snapshot,
                started_at=self.started_at,
                taken_at=now,
                sensors=sensors,
                pack=pack_state,
            )
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
