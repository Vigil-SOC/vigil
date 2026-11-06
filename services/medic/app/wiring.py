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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from services.medic.api.incidents import _folds
from services.medic.app.policy_probe import Policy, PolicyCheck
from services.medic.app.trace import TraceCapture
from services.medic.engine import (
    Engine,
    LoadedRule,
    load_rule,
    load_rule_file,
    suppress,
)
from services.medic.engine.core import BUILTINS, OVERFLOW
from services.medic.engine.history import gkey
from services.medic.packs import lifecycle
from services.medic.packs.loader import read_active, vet_pack
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
from services.medic.sensors.logtail import LOG_CAPACITY, LogStream, bind_log_tails
from services.medic.store import DecisionWriter, RecordHeld, StoreError, open_reader

log = logging.getLogger("services.medic")

TICK_S = 15  # semantics.md §1
PACK_DIR = Path(__file__).resolve().parents[1] / "packs" / "medic-core"  # dev mode only
# The image's bundled pack: a fixed name, no setting.
BUNDLED_DIR = Path(__file__).resolve().parents[1] / "packs" / "bundled"
VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"
# Several Helm series share this signal. Ungrouped, the newest read is
# ambiguous_series and the rule never fires. Compose and host-native have no
# pod label, so the file stays ungrouped and Helm adds the group at load.
WORKER_NOT_READY = "pipeline.agent-worker-not-ready"
GROUP_CAP = 64  # live groups per rule (semantics.md §3)
NETWORK_POLICY = "medic_network_policy"
RECORDING_STOPPED = "watcher.recording-stopped"
HELD_MAX = 1000  # decisions kept in memory while recording is stopped


class PackContext(NamedTuple):
    """What every later admin operation needs: the image's bundled bytes (None when
    the build has none) and Vigil's version."""

    bundled: bytes | None
    vigil: str


class Loaded(NamedTuple):
    """What the engine and sensors take from a rule pack."""

    rules: list[LoadedRule]
    suppression: list[dict[str, Any]]
    catalog: set[tuple[str, str]]
    # The active pack's PackFacts (status maps them to a PackBrief); None in dev.
    facts: dict[str, Any] | None = None
    events: Sequence[dict[str, Any]] = ()  # pack_event bodies for the decision log
    context: PackContext | None = None  # None in dev mode: no lifecycle
    # Lane-1 rule id -> {id, verify_after_s}: what the router copies onto an incident.
    runbooks: Mapping[str, dict[str, Any]] = {}


def load_pack(
    shape: str,
    *,
    dev_mode: bool,
    data_dir: Path,
    now: datetime,
    vigil_version: str | None = None,
) -> Loaded:
    """The rules Medic runs. Dev mode: the medic-core source tree. Otherwise: the
    checked active pack, after the lifecycle's start-up work (bundled seed or
    upgrade). With no usable pack it is
    empty, never an error: the built-in watcher.* rules still run. Dev mode lets a
    bad rule raise, which stops the start."""
    if dev_mode:
        pack_doc = yaml.safe_load((PACK_DIR / "pack.yaml").read_text())["pack"]
        files = sorted((PACK_DIR / "rules").glob("*.yaml"))
        if not files:
            raise FileNotFoundError(f"no rules in {PACK_DIR / 'rules'}")
        pack = (pack_doc["id"], pack_doc["channel"], pack_doc["version"])
        return _assemble(
            shape,
            pack,
            [load_rule_file(p, pack=pack) for p in files],
            yaml.safe_load((PACK_DIR / "suppression.yaml").read_text()),
            yaml.safe_load((PACK_DIR / "catalog.yaml").read_text()),
            runbooks=_runbook_map(
                shape,
                [
                    yaml.safe_load(p.read_text())
                    for p in sorted((PACK_DIR / "runbooks").glob("*.yaml"))
                ],
            ),
        )
    try:
        return _load_active(shape, data_dir, now, vigil_version)
    except Exception as exc:  # noqa: BLE001 (a bad pack never crash-loops Medic)
        return _no_pack(f"loading it failed ({type(exc).__name__}: {exc})")


def _no_pack(reason: str, events: Sequence[dict[str, Any]] = ()) -> Loaded:
    """`events`: what the lifecycle already did, still owed to the decision log."""
    log.warning(
        "Medic has no rule pack (%s): only the built-in watcher.* rules run", reason
    )
    return Loaded([], [], set(), events=events)


def _runbook_map(shape: str, descriptors: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Rule id -> {id, verify_after_s}, one entry per rule a descriptor applies to.
    The record's runbook object takes nothing else. A stop_stale_listener runbook
    is only for start_sh, so a later pack carrying one stays out of other shapes."""
    return {
        rid: {"id": rb["id"], "verify_after_s": rb["verify_after_s"]}
        for rb in descriptors
        if rb["action"]["type"] != "stop_stale_listener" or shape == "start_sh"
        for rid in rb["applies_to"]
    }


def _assemble(shape, pack, rules, suppression_doc, catalog_doc, **extra) -> Loaded:
    if shape == "helm":
        rules = [
            _group_worker_by_pod(rule) if rule.id == WORKER_NOT_READY else rule
            for rule in rules
        ]
    # `why` is the pack's note to a human; the engine's shape check refuses it.
    suppression = [
        {k: v for k, v in e.items() if k != "why"}
        for e in (suppression_doc or {}).get("entries") or []
    ]
    entries = (catalog_doc or {}).get("entries") or []
    catalog = {(e["logger"], e["template"]) for e in entries}
    return Loaded(rules, suppression, catalog, **extra)


def _load_active(
    shape: str, data_dir: Path, now: datetime, vigil_version: str | None
) -> Loaded:
    try:
        bundled = (BUNDLED_DIR / "medic-core.json").read_bytes()
    except OSError:
        bundled = None
    vigil = vigil_version or VERSION_FILE.read_text().strip()
    # Seed errors are logged by the lifecycle, once per code.
    started = lifecycle.start(
        data_dir=data_dir, bundled=bundled, vigil_version=vigil, now=now
    )
    context = PackContext(bundled, vigil)
    return reload_active(shape, data_dir, now, context, started.events)


def reload_active(
    shape: str,
    data_dir: Path,
    now: datetime,
    context: PackContext,
    events: Sequence[dict[str, Any]] = (),
) -> Loaded:
    """The active pack, loaded again (start-up, and after an admin swap). Never
    raises: `events` are already owed, and a bad pack never crash-loops Medic."""
    try:
        loaded = _from_active(
            shape, data_dir, now, context.vigil, context.bundled, events
        )
    except Exception as exc:  # noqa: BLE001
        loaded = _no_pack(f"loading it failed ({type(exc).__name__}: {exc})", events)
    return loaded._replace(context=context)


def _from_active(shape, data_dir, now, vigil, bundled, events) -> Loaded:
    active = read_active(data_dir)
    if active is None:
        return _no_pack("no pack is active", events)
    # Checked again, as the bundled pack is (stale and out-of-range run).
    errors, res = vet_pack(
        active.data, vigil_version=vigil, now=now, lineage=None,
        tolerate=lifecycle.TOLERATED,
    )  # fmt: skip
    if errors:
        return _no_pack(
            f"the active pack did not check: {[c for c, _ in errors]}", events
        )
    m = res.manifest["pack"]
    pack = (m["id"], m["channel"], m["version"])
    files = json.loads(active.data)["files"]
    rules = [load_rule(doc, pack=pack) for doc in res.rules.values()]
    state = lifecycle.pack_state(
        data_dir=data_dir,
        bundled=bundled,
        vigil_version=vigil,
        now=now,
    )
    return _assemble(
        shape, pack, rules,
        yaml.safe_load(files.get("suppression.yaml", "entries: []")),
        yaml.safe_load(files.get("catalog.yaml", "entries: []")),
        facts=state["active"], events=events,
        runbooks=_runbook_map(shape, [
            yaml.safe_load(text) for path, text in sorted(files.items())
            if path.startswith("runbooks/")
        ]),
    )  # fmt: skip


def _group_worker_by_pod(loaded: LoadedRule) -> LoadedRule:
    doc = copy.deepcopy(loaded.rule)
    doc["signals"]["ready"]["sample"]["by"] = ["pod"]
    doc["group_by"] = ["pod"]
    return load_rule(doc, pack=loaded.pack)


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
            # If that tick was already evaluated (a quick restart, a clock step
            # back), `evaluate` skips it and the engine sees this on the next.
            tick, last = grid_tick(self.clock), self.engine.last_tick
            if last is not None and tick <= last:
                tick = int(last) + TICK_S
            self.capture.write(observation, tick)


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


class StoreResetSensor:
    """`medic_store`/`reset`: true on the first collect of a start that reset the
    store after corruption, false after. The built-in watcher.store-reset rule
    opens its incident on true and keeps it on the console for 24 h."""

    id = "medic.store-reset"
    service = "medic"
    interval_s = 60
    timeout_s = 5.0
    covers = ("medic_store",)
    uses_vigil_api = False

    def __init__(self, writer: DecisionWriter) -> None:
        self.writer, self._reported = writer, False

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        flag = self.writer.reset_on_open and not self._reported
        self._reported = True
        return [Reading("medic_store", self.service, [Value.flag("reset", flag)])]


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
        runbooks: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.writer, self.clock = writer, clock
        self.shape, self.group_cap = shape, group_cap
        self.capture = capture or TraceCapture()
        self.engine = Engine(
            list(rules),
            instance_id=writer.instance_id,
            install_shape=shape,
            state=engine_state,
            suppression=[dict(e) for e in suppression],
            group_cap=group_cap,
        )
        self.router = Router(rules, runbooks=runbooks)
        self.bus = Bus(Stats())
        self.log_bus = Bus(self.bus.stats, LOG_CAPACITY)
        bind_log_tails(sensors, self.log_bus, clock)
        self.sink = EngineSink(self.engine, self.capture, clock)
        self.sensors = list(sensors)
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
        # The newest record this process stored or found at start; -1: none yet.
        self.head_seq = -1

    def state(self) -> dict[str, Any]:
        """What the next start loads: the engine's state and the store head it matches
        (a start finding the store past it rebuilds the open incidents)."""
        return self.engine.state() | {"head_seq": self.head_seq}

    def swap(self, pack: Loaded) -> None:
        """Run a new pack from the next tick. Called between cycles. The engine
        keeps its state, so a rule the pack dropped ends its open incidents as
        `rule_retired` on the next tick; the sensors stay, the log tails get the
        new catalogue."""
        old = self.engine
        self.engine = Engine(
            list(pack.rules),
            instance_id=self.writer.instance_id,
            install_shape=self.shape,
            state=old.state(),
            suppression=[dict(e) for e in pack.suppression],
            group_cap=self.group_cap,
        )
        # What the old engine has seen (rule-independent) goes with it: the sensors
        # aren't re-read until their next interval, so a fresh history would leave
        # every window empty until then. `state()` above carried the rest.
        for part in ("pending", "history", "upgrade", "_status"):
            setattr(self.engine, part, getattr(old, part))
        self.router = Router(pack.rules, runbooks=pack.runbooks)
        self.sink.engine = self.engine
        for tail in {s.tail for s in self.sensors if isinstance(s, LogStream)}:
            tail.catalog = pack.catalog

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
        self.head_seq = stored["seq"]
        log.info("%s %s seq %d", routed["type"], body.get("incident_id"), stored["seq"])
        return stored


def _ts(iso_text: str) -> float:
    return datetime.fromisoformat(iso_text).timestamp()


def store_head(data_dir: Path) -> int:
    """The store's newest seq (-1 when empty: the first record is seq 0)."""
    with open_reader(data_dir) as conn:
        return conn.execute("SELECT coalesce(max(seq), -1) FROM records").fetchone()[0]


def rebuild_machines(
    data_dir: Path, state: dict[str, Any] | None, rules: Sequence[LoadedRule]
) -> tuple[dict[str, Any], int]:
    """The engine state with its incident machines rebuilt from the store: the
    store is the truth, the state a cache (a crash can land between a record's append
    and the state's save). An incident the store holds open resumes with its stored
    id; a machine holding one the store doesn't list as open goes back to inactive.
    Latches, cover, upgrade and last_tick stay. Returns (state, incidents resumed)."""
    state = copy.deepcopy(state) if state else {"v": 1, "machines": {}}
    machines = state["machines"]
    by_id = {r.id: r for r in rules}
    with open_reader(data_dir) as conn:
        folds, _ = _folds(conn)
    for slots in machines.values():
        for m in slots.values():
            if m.get("incident_id"):
                kept = {k: m[k] for k in ("seen", "opens", "members", "last") if k in m}
                m.clear()
                m.update(kept, state="inactive")
    resumed = 0
    for fold in folds.values():
        o = fold.opened["body"]
        if fold.resolved is not None or o["rule"] is None:  # (G1 R4: no rule to run)
            continue
        rule_id = o["rule"]["id"]
        key = gkey({g["name"]: g["value"] for g in o["group"]})
        old = machines.setdefault(rule_id, {}).get(key, {})
        # The engine's evidence names are the rule's signal names; a built-in's
        # (and a gone rule's) are the signals themselves.
        rule = by_id.get(rule_id)
        signals = {e["signal"] for e in o["evidence"]} | fold.evidence
        names = signals
        if rule and rule_id not in BUILTINS:
            names = {
                n
                for n, sig in rule.rule["signals"].items()
                if next(iter(sig.values()))["signal"] in signals
            }
        opened_at = _ts(fold.opened["at"])
        m = {k: old[k] for k in ("seen", "opens", "members", "last") if k in old}
        if o["group"] and all(g["value"] == OVERFLOW for g in o["group"]):
            m.setdefault("members", {})  # the overflow group: its members re-form
        machines[rule_id][key] = (
            m
            | {
                "state": "resolving" if fold.resolving else "firing",
                "incident_id": o["incident_id"],
                "active_since": _ts(o["active_since"]),
                "opened_at": opened_at,
                "routed_at": _ts(fold.routed_at) if fold.routed_at else None,
                "hold_end": opened_at + suppress.HOLD_S,
                "suppressed_by": fold.suppressed_by,
                "reopen_count": fold.reopen_count,
                "evidenced": sorted(names),
                "seen": max(old.get("seen", 0), _ts(fold.last_at or fold.opened["at"])),
            }
            | ({"resolving_since": _ts(fold.resolving_at)} if fold.resolving else {})
        )
        if fold.reopen_count:
            machines[rule_id][key]["flapping"] = True
        resumed += 1
    return state, resumed


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
