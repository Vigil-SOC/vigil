"""One Medic, wired: sensors → bus → K2 choke → engine (15 s tick) → router R1 → store.

Each cycle the scheduler starts the sensors that are due, the pipeline hands every
finished, redacted observation to the engine, and the engine evaluates the tick on
the 15 s grid. The engine (E3 complete: suppression, group cap, upgrade windows)
writes each record's routing, `route` included; the router adds lane, runbook and
would_have; the store's single writer chains. A read that finishes between cycles
reaches the engine on the next one.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from services.medic.engine import Engine, LoadedRule, load_rule_file
from services.medic.router import Router
from services.medic.sensors import (
    Bus,
    Pipeline,
    Scheduler,
    Sensor,
    SensorContext,
    Stats,
)
from services.medic.sensors.base import Clock
from services.medic.store import DecisionWriter, StoreError

log = logging.getLogger("services.medic")

TICK_S = 15  # semantics.md §1
DEV_RULES_DIR = Path(__file__).resolve().parents[1] / "rules" / "dev"
DEV_SUPPRESSION = DEV_RULES_DIR / "suppression.yaml"
GROUP_CAP = 64  # live groups per rule (semantics.md §3)
ENGINE_STATE_VERSION = 1


def load_dev_rules(directory: Path = DEV_RULES_DIR) -> list[LoadedRule]:
    """Dev-mode rules through the S4b loader. No pack loader, no signatures (F6)."""
    return [
        load_rule_file(p)
        for p in sorted(directory.glob("*.yaml"))
        if p.name != DEV_SUPPRESSION.name
    ]


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


class EngineSink:
    """The pipeline's sink: every redacted, checked observation goes to the engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def write(self, observation: Mapping[str, Any]) -> None:
        self.engine.observe(dict(observation))


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
    ) -> None:
        self.writer, self.clock = writer, clock
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
        self.sink = EngineSink(self.engine)
        self.pipeline = Pipeline(self.bus, self.sink)
        self.scheduler = Scheduler(sensors, SensorContext(shape=shape), self.bus, clock)
        self.unstored = 0  # records the store refused or couldn't take
        self._stepped_back = False

    async def cycle(self) -> list[dict[str, Any]]:
        """Start due sensors, hand finished reads to the engine, evaluate the tick."""
        await self.scheduler.tick()
        self.pipeline.drain()
        return self.evaluate()

    def evaluate(self) -> list[dict[str, Any]]:
        """Run the engine on the latest grid tick not yet evaluated; store its records."""
        tick = int(self.clock.wall()) // TICK_S * TICK_S
        last = self.engine.last_tick
        if last is not None and tick <= last:
            if tick < last - TICK_S and not self._stepped_back:
                self._stepped_back = True
                log.warning("Wall clock is %d s behind the last tick", last - tick)
            return []  # same tick again, or the wall clock stepped back
        self._stepped_back = False
        stored = []
        for record in self.engine.tick(datetime.fromtimestamp(tick, UTC)):
            routed = {"v": 1, **self.router.route(record)}
            try:
                stored.append(self.writer.append(routed))
            # C5: the loop keeps beating whatever state the store is in (G3 adds
            # "recording stopped"); a lost record is counted and said once each.
            except (StoreError, sqlite3.Error) as exc:
                self.unstored += 1
                log.error(
                    "Decision record not stored: %s %s (%s)",
                    record["type"],
                    record["body"]["incident_id"],
                    getattr(exc, "code", type(exc).__name__),
                )
                continue
            log.info(
                "%s %s seq %d",
                record["type"],
                record["body"]["incident_id"],
                stored[-1]["seq"],
            )
        return stored


def engine_state_path(data_dir: Path) -> Path:
    return data_dir / "run" / "engine-state.json"


def save_engine_state(data_dir: Path, state: dict[str, Any]) -> None:
    """Atomically (temp file, then rename), private to Medic's uid."""
    path = engine_state_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    doc = {"v": ENGINE_STATE_VERSION, "engine": state}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".engine-state.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, sort_keys=True, allow_nan=False)
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
    if (
        not isinstance(doc, dict)
        or doc.get("v") != ENGINE_STATE_VERSION
        or not isinstance(doc.get("engine"), dict)
    ):
        log.warning("Engine state has an unknown version, starting fresh")
        return None
    return doc.get("engine")
