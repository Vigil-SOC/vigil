"""Runs sensors on fixed intervals and makes sure a dead one is never silence.

Every 15 s tick (semantics §1) the scheduler:

- starts each sensor that is due, at most one `collect()` per sensor at a time;
- cuts a call past its `timeout_s` (checked on the tick, so a fake clock drives it,
  and by `asyncio.timeout` in real time): that is a failure, `error.class: timeout`;
- turns a crash into a failure, `error.class: other`, detail = the exception type only;
- for every failure, writes an `outcome: error` sample for each signal the sensor
  covers, plus a framework `sensor_health`; so a hang or crash shows within one tick;
- reports `blind` after 3 failed cycles in a row and `stopped` when a sensor has
  not finished a cycle for 3 intervals (observation.schema.json `health.state`);
- writes `sensor_health` for every sensor at least once per interval, also while
  backing off or stuck.

Backoff (C7 §4) applies only after 3 `outcome: error` cycles in a row: the gap
doubles up to 4 × interval, never past 5 min (and never below the interval),
±10% jitter, reset on success. A read that worked and says Vigil is down is a
result, not an error, and never backs off.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.medic.sensors.base import (
    Clock,
    ReadError,
    Reading,
    Sensor,
    SensorContext,
    check_sensor,
)
from services.medic.sensors.bus import Bus

log = logging.getLogger("services.medic.sensors")

TICK_S = 15
BLIND_AFTER = 3  # failed cycles in a row
STOPPED_AFTER = 3  # intervals without a finished cycle
BACKOFF_MAX_S = 300
FRAMEWORK_ID = "medic.framework"


def iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class _State:
    sensor: Sensor
    next_due: float
    last_done: float  # monotonic: last finished (or failed) cycle, or scheduler start
    last_health: float = float("-inf")
    task: asyncio.Task[None] | None = None
    started: float = 0.0
    cut: bool = False
    consecutive_failures: int = 0
    last_ok_at: str | None = None
    last_error: ReadError | None = None
    dropped_seen: int = 0
    off_reason: str | None = None


class Scheduler:
    def __init__(
        self,
        sensors: Sequence[Sensor],
        ctx: SensorContext,
        bus: Bus,
        clock: Clock,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.ctx, self.bus, self.clock = ctx, bus, clock
        self._rng = rng or random.Random()
        now = clock.monotonic()
        self._states: dict[str, _State] = {}
        for sensor in sensors:
            check_sensor(sensor)
            if sensor.id in self._states or sensor.id == FRAMEWORK_ID:
                raise ValueError(f"duplicate sensor id {sensor.id!r}")
            st = _State(sensor=sensor, next_due=now, last_done=now)
            if ctx.vigil_dev_mode and sensor.uses_vigil_api:
                # K1 T-37: on a DEV_MODE install every request is an admin, so the
                # API sensors stay off, with the reason on /status.
                st.off_reason = "vigil_dev_mode"
                log.warning("Sensor %s is off: Vigil runs with DEV_MODE on", sensor.id)
            self._states[sensor.id] = st

    def status(self) -> dict[str, Any]:
        """C5 §5.4 counts for `GET /status`."""
        states = [s for s in self._states.values() if s.off_reason is None]
        return {
            "sensors": len(self._states),
            "off": {
                s.sensor.id: s.off_reason for s in self._states.values() if s.off_reason
            },
            "blind": sorted(
                s.sensor.id
                for s in states
                if self._health_state(s) in ("blind", "stopped")
            ),
            "dropped": self.bus.stats.total("dropped"),
            "redactor_failures": self.bus.stats.total("redactor_failures"),
            "vigil_dev_mode": self.ctx.vigil_dev_mode,
        }

    async def run(self, drain: Any, stop: asyncio.Event) -> None:
        """The production loop (S4 wires `drain` to the pipeline)."""
        while not stop.is_set():
            await self.tick()
            drain()
            await self.clock.sleep(TICK_S)

    async def tick(self) -> None:
        now = self.clock.monotonic()
        for st in self._states.values():
            if st.off_reason is not None:
                continue
            if st.task is not None and not st.task.done():
                if not st.cut and now >= st.started + st.sensor.timeout_s:
                    st.cut = True
                    st.task.cancel()
                    self._fail(
                        st,
                        ReadError(
                            "timeout",
                            detail=f"collect() ran past {st.sensor.timeout_s:g} s",
                        ),
                    )
            elif now >= st.next_due:
                self._start(st, now)
        await asyncio.sleep(0)  # let the calls just started run to their first await
        now = self.clock.monotonic()
        for st in self._states.values():
            if st.off_reason is not None or now - st.last_health < st.sensor.interval_s:
                continue
            running = st.task is not None and not st.task.done()
            if not running or now >= st.started + st.sensor.timeout_s:
                self._health(st, reported_by="framework")

    # -- one cycle ---------------------------------------------------------

    def _start(self, st: _State, now: float) -> None:
        st.started, st.cut = now, False
        self.bus.stats[st.sensor.id].reads += 1
        st.task = asyncio.get_running_loop().create_task(
            self._call(st), name=f"medic.sensor.{st.sensor.id}"
        )

    async def _call(self, st: _State) -> None:
        sensor = st.sensor
        try:
            async with asyncio.timeout(sensor.timeout_s):
                readings = await sensor.collect(self.ctx)
            drafts = [self._sample(st, r) for r in readings]
        except TimeoutError:
            if not st.cut:
                st.cut = True
                self._fail(
                    st,
                    ReadError(
                        "timeout", detail=f"collect() ran past {sensor.timeout_s:g} s"
                    ),
                )
            return
        except asyncio.CancelledError:
            if st.cut:
                return  # we cut it; the tick already recorded the failure
            raise
        # A crashing sensor is a counted failure, whatever it raised.
        except Exception as exc:  # noqa: BLE001
            # The type only: an exception message can carry what the sensor read.
            log.warning("Sensor %s failed: %s", sensor.id, type(exc).__name__)
            self._fail(
                st, ReadError("other", detail=f"collect() raised {type(exc).__name__}")
            )
            return
        if st.cut:
            return  # finished after we gave up on it: too late to count
        for draft in drafts:
            self.bus.put(draft)
        errors = [r.error for r in readings if r.error is not None]
        if readings and len(errors) == len(readings):
            self._failed_cycle(st, errors[-1])
        else:
            st.consecutive_failures = 0
            st.last_ok_at = iso(self.clock.wall())
            st.last_error = errors[-1] if errors else None
            self._schedule(st, backoff=False)
        st.last_done = self.clock.monotonic()
        self._health(st, reported_by="sensor", partial=bool(errors))

    def _fail(self, st: _State, error: ReadError) -> None:
        """A cycle that produced nothing: crash or timeout."""
        for signal in st.sensor.covers:
            self.bus.put(
                self._draft(
                    st,
                    signal,
                    st.sensor.service,
                    outcome="error",
                    error=error.to_json(),
                    values=[],
                )
            )
        self._failed_cycle(st, error)
        st.last_done = self.clock.monotonic()
        self._health(st, reported_by="framework")

    def _failed_cycle(self, st: _State, error: ReadError) -> None:
        self.bus.stats[st.sensor.id].failures += 1
        st.consecutive_failures += 1
        st.last_error = error
        self._schedule(st, backoff=True)

    def _schedule(self, st: _State, *, backoff: bool) -> None:
        interval = st.sensor.interval_s
        gap = float(interval)
        if backoff and st.consecutive_failures >= BLIND_AFTER:
            factor = min(2 ** (st.consecutive_failures - BLIND_AFTER + 1), 4)
            gap = max(interval, min(interval * factor, BACKOFF_MAX_S))
            if gap > interval:
                gap *= self._rng.uniform(0.9, 1.1)
        st.next_due = st.started + gap

    # -- observations ------------------------------------------------------

    def _health_state(self, st: _State, partial: bool = False) -> str:
        now = self.clock.monotonic()
        running = st.task is not None and not st.task.done()
        if running and now - st.last_done >= STOPPED_AFTER * st.sensor.interval_s:
            return "stopped"
        if st.consecutive_failures >= BLIND_AFTER:
            return "blind"
        dropped = (
            self.bus.stats[st.sensor.id].dropped
            + self.bus.stats[st.sensor.id].redactor_failures
        )
        if (
            st.consecutive_failures
            or partial
            or dropped > st.dropped_seen
            or st.last_ok_at is None
        ):
            return "degraded"
        return "ok"

    def _health(self, st: _State, *, reported_by: str, partial: bool = False) -> None:
        state = self._health_state(st, partial)
        if state == "stopped":
            reported_by = "framework"
        stats = self.bus.stats[st.sensor.id]
        st.dropped_seen = stats.dropped + stats.redactor_failures
        st.last_health = self.clock.monotonic()
        health: dict[str, Any] = {
            "sensor": st.sensor.id,
            "state": state,
            "reported_by": reported_by,
            "covers": list(st.sensor.covers),
            "interval_s": st.sensor.interval_s,
            "last_ok_at": st.last_ok_at,
            "consecutive_failures": st.consecutive_failures,
            "counters": stats.to_json(),
        }
        if st.last_error is not None:
            health["last_error"] = st.last_error.to_json()
        emitter = st.sensor.id if reported_by == "sensor" else FRAMEWORK_ID
        draft = self._draft(
            st,
            "sensor_health",
            "medic",
            outcome="ok",
            kind="sensor_health",
            emitter=emitter,
        )
        draft["health"] = health
        self.bus.put_health(draft)

    def _sample(self, st: _State, reading: Reading) -> dict[str, Any]:
        if reading.signal not in st.sensor.covers:
            raise ValueError(
                f"signal {reading.signal!r} isn't in {st.sensor.id}.covers"
            )
        if reading.error is not None and reading.values:
            raise ValueError("a failed read carries no values")
        draft = self._draft(
            st,
            reading.signal,
            reading.service,
            outcome="error" if reading.error else "ok",
            values=[v.to_json() for v in reading.values],
            instance=reading.instance,
            source_ts=reading.source_ts,
        )
        if reading.error is not None:
            draft["error"] = reading.error.to_json()
        if reading.epoch is not None:
            draft["epoch"] = reading.epoch
        return draft

    def _draft(
        self,
        st: _State,
        signal: str,
        service: str,
        *,
        outcome: str,
        kind: str = "sample",
        emitter: str | None = None,
        error: dict[str, Any] | None = None,
        values: list[dict[str, Any]] | None = None,
        instance: str | None = None,
        source_ts: str | None = None,
    ) -> dict[str, Any]:
        now = iso(self.clock.wall())
        target: dict[str, Any] = {"service": service, "shape": self.ctx.shape}
        if instance is not None:
            target["instance"] = instance
        draft: dict[str, Any] = {
            "_emitter": emitter or st.sensor.id,
            "_subject": st.sensor.id,
            "kind": kind,
            "signal": signal,
            "target": target,
            "t": now,
            "observed_at": now,
            "source_ts": source_ts,
            "outcome": outcome,
        }
        if error is not None:
            draft["error"] = error
        if values is not None:
            draft["values"] = values
        return draft
