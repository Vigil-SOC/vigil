"""One Medic, wired: sensors → bus → K2 choke → engine (15 s tick) → router → store.

Each cycle the scheduler starts the sensors that are due, the pipeline hands every
finished, redacted observation to the engine, and the engine evaluates the tick on
the 15 s grid. The engine (E3 complete: suppression, group cap, upgrade windows)
writes each record's routing, `route` included; the router adds lane, runbook and
would_have; the store's single writer chains. A read that finishes between cycles
reaches the engine on the next one.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from services.medic.app.policy_probe import Policy, PolicyCheck
from services.medic.app.trace import TraceCapture
from services.medic.engine import Engine, LoadedRule, load_rule, load_rule_file
from services.medic.router import Router
from services.medic.sensors import (
    Bus,
    Pipeline,
    ReadError,
    Reading,
    Scheduler,
    Sensor,
    SensorContext,
    Stats,
    Value,
)
from services.medic.sensors.base import Clock
from services.medic.sensors.logtail import LOG_CAPACITY, bind_log_tails
from services.medic.store import DecisionWriter, RecordHeld, StoreError

log = logging.getLogger("services.medic")

TICK_S = 15  # semantics.md §1
DEV_RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "dev"
DEV_SUPPRESSION = DEV_RULES_DIR / "suppression.yaml"
# Several Helm series share this signal. Ungrouped, the newest read is
# ambiguous_series and the rule never fires. Compose and host-native have no
# pod label, so the file stays ungrouped and Helm adds the group at load.
WORKER_NOT_READY = "pipeline.agent-worker-not-ready"
GROUP_CAP = 64  # live groups per rule (semantics.md §3)
NETWORK_POLICY = "medic_network_policy"
RECORDING_STOPPED = "watcher.recording-stopped"
HELD_MAX = 1000  # decisions kept in memory while recording is stopped


def load_dev_rules(directory: Path = DEV_RULES_DIR) -> list[LoadedRule]:
    """Dev-mode rules through the S4b loader. No pack loader, no signatures (F6)."""
    return [
        load_rule_file(p)
        for p in sorted(directory.glob("*.yaml"))
        if p.name != DEV_SUPPRESSION.name
    ]


def rules_for_shape(shape: str, directory: Path = DEV_RULES_DIR) -> list[LoadedRule]:
    """`load_dev_rules`, with the worker rule grouped by pod on Helm."""
    rules = load_dev_rules(directory)
    if shape != "helm":
        return rules
    return [
        _group_worker_by_pod(rule) if rule.id == WORKER_NOT_READY else rule
        for rule in rules
    ]


def _group_worker_by_pod(loaded: LoadedRule) -> LoadedRule:
    doc = copy.deepcopy(loaded.rule)
    doc["signals"]["ready"]["sample"]["by"] = ["pod"]
    doc["group_by"] = ["pod"]
    return load_rule(doc, pack=loaded.pack)


def load_dev_suppression(path: Path = DEV_SUPPRESSION) -> list[dict[str, Any]]:
    """The dev set's suppression.yaml entries (E3 §6); the pack's, once F6 loads
    packs. The engine shape-checks every entry and refuses a malformed one."""
    if not path.exists():
        return []
    entries = yaml.safe_load(path.read_text())
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise TypeError(f"{path.name}: expected a list of suppression entries")
    return entries


def grid_tick(clock: Clock) -> int:
    """The 15 s grid tick the wall clock is in (semantics.md §1)."""
    return int(clock.wall()) // TICK_S * TICK_S


class EngineSink:
    """The pipeline's sink: every redacted, checked observation goes to the engine,
    then to trace capture, which can't fail or hold up the engine."""

    def __init__(
        self,
        engine: Engine,
        capture: TraceCapture | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.engine, self.capture, self.clock = engine, capture, clock

    def write(self, observation: Mapping[str, Any]) -> None:
        self.engine.observe(dict(observation))
        if self.capture is not None and self.clock is not None:
            # `drain` runs just before `evaluate`: this is the tick it will see.
            self.capture.write(observation, grid_tick(self.clock))


class RecordingSensor:
    """`medic_recording`/`stopped`: whether the store has stopped recording. The
    built-in watcher.recording-stopped rule opens its incident on true and
    resolves it on false."""

    id = "medic.recording"
    service = "medic"
    interval_s = 60
    timeout_s = 5.0
    covers = ("medic_recording",)
    uses_vigil_api = False

    def __init__(self, writer: DecisionWriter) -> None:
        self.writer = writer

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        stopped = self.writer.stopped
        return [
            Reading("medic_recording", self.service, [Value.flag("stopped", stopped)])
        ]


class PolicyStartupSensor:
    """`medic_network_policy`/`unenforced`, once, after a startup refusal that
    lasted: true, then false on the next collect, so the incident opens and
    resolves within a minute of the clean start. Then it reads nothing."""

    id = "medic.network-policy-start"
    service = "medic"
    interval_s = 30
    timeout_s = 5.0
    covers = ("medic_network_policy",)
    uses_vigil_api = False

    def __init__(self) -> None:
        self._reads = [True, False]

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        if not self._reads:
            return []
        flag = self._reads.pop(0)
        return [Reading(NETWORK_POLICY, self.service, [Value.flag("unenforced", flag)])]


class PolicySensor:
    """The startup NetworkPolicy check, re-run once after start and every 6 h
    (Helm). Losing enforcement mid-run opens watcher.policy-unenforced. A check
    that couldn't tell (control down, inconclusive probe) is an error read, never
    `unenforced`. `skip_first`: the startup sensor owns the first read."""

    id = "medic.network-policy"
    service = "medic"
    interval_s = 21_600
    # The check retries the control for ~90 s and the target for ~20 s.
    timeout_s = 150.0
    covers = (NETWORK_POLICY,)
    uses_vigil_api = False

    def __init__(
        self, check: Callable[[], PolicyCheck], *, skip_first: bool = False
    ) -> None:
        self.check, self._skip = check, skip_first

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        if self._skip:
            self._skip = False
            return []
        found = await asyncio.to_thread(self.check)  # blocks: off the event loop
        if found.state is Policy.UNKNOWN:
            log.warning("NetworkPolicy re-check: %s", found.text)
            return [Reading(NETWORK_POLICY, self.service, error=ReadError("other"))]
        if found.state is Policy.UNENFORCED:
            log.error("NetworkPolicy re-check: %s", found.text)
        return [
            Reading(
                NETWORK_POLICY,
                self.service,
                [Value.flag("unenforced", found.state is Policy.UNENFORCED)],
            )
        ]


class Medic:
    def __init__(
        self,
        *,
        writer: DecisionWriter,
        rules: Sequence[LoadedRule],
        sensors: Sequence[Sensor],
        clock: Clock,
        shape: str,
        engine_state: dict[str, Any] | None = None,
        suppression: Sequence[Mapping[str, Any]] = (),
        group_cap: int = GROUP_CAP,
        vigil_dev_mode: bool = False,
        capture: TraceCapture | None = None,
    ) -> None:
        self.writer, self.clock = writer, clock
        self.capture = capture or TraceCapture()
        self.engine = Engine(
            list(rules),
            instance_id=writer.instance_id,
            install_shape=shape,
            state=engine_state,
            suppression=[dict(e) for e in suppression],
            group_cap=group_cap,
        )
        self.router = Router(rules)
        self.bus = Bus(Stats())
        self.log_bus = Bus(self.bus.stats, LOG_CAPACITY)
        bind_log_tails(sensors, self.log_bus, clock)
        self.sink = EngineSink(self.engine, self.capture, clock)
        self.pipeline = Pipeline(self.bus, self.sink, log_bus=self.log_bus)
        self.scheduler = Scheduler(
            sensors,
            SensorContext(shape=shape, vigil_dev_mode=vigil_dev_mode),
            self.bus,
            clock,
        )
        self.unstored = 0  # records the store refused or couldn't take
        # Decisions held while recording is stopped (not durable: a crash drops
        # them, uncounted) and the incidents allowed into the reserve.
        self.held: list[dict[str, Any]] = []
        self._reserved: set[str] = set()
        self._stepped_back = False

    async def cycle(self) -> list[dict[str, Any]]:
        """Start due sensors, hand finished reads to the engine, evaluate the tick."""
        await self.scheduler.tick()
        self.pipeline.drain()
        return self.evaluate()

    def evaluate(self) -> list[dict[str, Any]]:
        """Run the engine on the latest grid tick not yet evaluated; store its records."""
        tick = grid_tick(self.clock)
        last = self.engine.last_tick
        if last is not None and tick <= last:
            if tick < last - TICK_S and not self._stepped_back:
                self._stepped_back = True
                log.warning("Wall clock is %d s behind the last tick", last - tick)
            return []  # same tick again, or the wall clock stepped back
        self._stepped_back = False
        self._resume()
        self.capture.check_disk(self.writer.stop_reason)
        stored = []
        records = self.engine.tick(datetime.fromtimestamp(tick, UTC))
        status = self.engine.status()  # this tick's runtime_untrusted flags (R3)
        self._reserved.update(
            row["incident_id"]
            for row in status
            if row["rule"] == RECORDING_STOPPED and row["incident_id"]
        )
        for record in records:
            routed = {"v": 1, **self.router.route(record, status)}
            if stored_record := self._store(routed):
                stored.append(stored_record)
        return stored

    def _resume(self) -> None:
        """Re-check the store's bounds; when recording is back, write the held
        decisions in order and clear the list."""
        if self.writer.refresh() or not self.held:
            return
        held, self.held = self.held, []
        log.warning("Recording resumed: writing %d held decisions", len(held))
        for routed in held:
            self._store(routed)  # held again if recording stops mid-flush

    def _store(self, routed: dict[str, Any]) -> dict[str, Any] | None:
        body = routed["body"]
        if (
            routed["type"] == "incident_opened"
            and body["rule"]["id"] == RECORDING_STOPPED
        ):
            self._reserved.add(body["incident_id"])
        try:
            stored = self.writer.append(
                routed, reserve=body.get("incident_id") in self._reserved
            )
        except RecordHeld:
            if len(self.held) < HELD_MAX:
                if not self.held:
                    log.warning("Recording is stopped: holding decisions in memory")
                self.held.append(routed)
            else:
                self.unstored += 1
                log.error(
                    "Decision record lost: recording is stopped and %d are held (%s %s)",
                    HELD_MAX,
                    routed["type"],
                    body.get("incident_id"),
                )
            return None
        # C5: the loop keeps beating whatever state the store is in; a lost
        # record is counted and said once each.
        except (StoreError, sqlite3.Error) as exc:
            self.unstored += 1
            log.error(
                "Decision record not stored: %s %s (%s)",
                routed["type"],
                body.get("incident_id"),
                getattr(exc, "code", type(exc).__name__),
            )
            return None
        log.info("%s %s seq %d", routed["type"], body.get("incident_id"), stored["seq"])
        return stored


def engine_state_path(data_dir: Path) -> Path:
    return data_dir / "run" / "engine-state.json"


def save_engine_state(data_dir: Path, state: dict[str, Any]) -> None:
    """Atomically (temp file, then rename), private to Medic's uid."""
    path = engine_state_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".engine-state.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, sort_keys=True, allow_nan=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_engine_state(data_dir: Path) -> dict[str, Any] | None:
    """The engine's last state, or None to start fresh (never a crash loop)."""
    path = engine_state_path(data_dir)
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Engine state unreadable, starting fresh: %s", type(exc).__name__)
        return None
    if isinstance(doc, dict) and isinstance(doc.get("engine"), dict):
        doc = doc["engine"]  # the older {"v", "engine"} wrapper
    if not isinstance(doc, dict) or doc.get("v") != 1:  # the engine's own version
        log.warning("Engine state has an unknown version, starting fresh")
        return None
    return doc
