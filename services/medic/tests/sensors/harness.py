"""Fake sensors and a rig that runs the framework on a fake clock."""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from services.medic.sensors import (
    Bus,
    MemorySink,
    Pipeline,
    Reading,
    Scheduler,
    Sensor,
    SensorContext,
    Stats,
    Value,
)
from services.medic.sensors.pipeline import VALIDATOR, Choke
from services.medic.tests.fakes import FakeClock


@dataclass
class FakeSensor:
    id: str
    service: str = "backend"
    interval_s: int = 30
    timeout_s: float = 5
    covers: tuple[str, ...] = ("api_health",)
    uses_vigil_api: bool = False
    mode: str = "ok"  # ok | crash | hang | stubborn | error
    calls: int = 0
    readings: Sequence[Reading] | None = None
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        self.calls += 1
        if self.mode == "crash":
            raise RuntimeError("password=canaryCrash0030 leaked into an exception")
        if self.mode == "hang":
            await self.release.wait()
        if self.mode == "stubborn":
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Ignores the scheduler's cancel; the second (asyncio.run's
                # shutdown) ends it, so the test can finish.
                await self.release.wait()
        if self.readings is not None:
            return self.readings
        if self.mode == "error":
            from services.medic.sensors import ReadError

            return [Reading(self.covers[0], self.service, error=ReadError("refused"))]
        return [Reading(self.covers[0], self.service, values=[Value.flag("ok", True)])]


def stamp_only(draft: dict[str, Any]) -> dict[str, Any]:
    """A choke that redacts nothing: the framework's tests don't depend on K2's.
    `Rig(..., choke=None)` runs the production default, K2's redaction choke."""
    return {**draft, "redaction": {"version": "none", "hits": 0}}


class Rig:
    def __init__(
        self,
        sensors: Sequence[Sensor],
        *,
        capacity: int = 1024,
        dev_mode: bool = False,
        choke: Choke | None = stamp_only,
    ) -> None:
        self.clock = FakeClock()
        self.stats = Stats()
        self.bus = Bus(self.stats, capacity)
        self.sink = MemorySink()
        runs = (f"run{n:013d}" for n in itertools.count())
        self.pipeline = Pipeline(self.bus, self.sink, choke, run_id=lambda: next(runs))
        ctx = SensorContext(shape="compose", vigil_dev_mode=dev_mode)
        self.scheduler = Scheduler(
            sensors, ctx, self.bus, self.clock, rng=random.Random(7)
        )

    async def tick(self, advance: float = 0) -> list[dict[str, Any]]:
        """Advance the clock, run one tick, let started calls finish, drain."""
        self.clock.advance(advance)
        before = len(self.sink.observations)
        await self.scheduler.tick()
        for _ in range(5):
            await asyncio.sleep(0)
        self.pipeline.drain()
        return self.sink.observations[before:]

    def health(self, sensor: str) -> list[dict[str, Any]]:
        return [
            o["health"]
            for o in self.sink.of("sensor_health")
            if o["health"]["sensor"] == sensor
        ]

    def assert_all_valid(self) -> None:
        for obs in self.sink.observations:
            errors = [e.message for e in VALIDATOR.iter_errors(obs)]
            assert errors == [], (obs, errors)
