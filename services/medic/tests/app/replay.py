"""Replay harness: a recorded observation sequence, a fake clock, a fresh store.

The observations go in where the sensor pipeline hands them over (`Medic.sink`),
so everything after that is the app's own path: engine tick on the 15 s grid,
router R1, the store writer. Replay stores hold engine records only and run under
the recording's instance id (decision-record.md §3, S0-2 (a)).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from services.medic.app.wiring import TICK_S, Medic
from services.medic.engine import LoadedRule
from services.medic.store import open_writer
from services.medic.tests.fakes import FakeClock


def seed_instance(data_dir: Path, instance_id: str) -> None:
    data_dir.mkdir(mode=0o700, exist_ok=True)
    path = data_dir / "instance_id"
    path.write_text(instance_id + "\n")
    path.chmod(0o600)


def replay(
    observations: Sequence[dict[str, Any]],
    rules: Sequence[LoadedRule],
    data_dir: Path,
    *,
    instance_id: str,
    start: datetime,
    seconds: int,
    shape: str = "compose",
    arrived: Sequence[float] | None = None,
    suppression: Sequence[dict[str, Any]] = (),
    group_cap: int = 64,
) -> None:
    """Tick from `start` for `seconds`, feeding each observation once it arrived:
    at `arrived[i]` (wall clock) for a live recording, else at its observed_at."""
    seed_instance(data_dir, instance_id)
    times = list(arrived) if arrived is not None else [_ts(o) for o in observations]
    pending = sorted(zip(times, observations, strict=True), key=lambda p: p[0])
    clock = FakeClock(wall=start.timestamp())
    with open_writer(data_dir) as writer:
        medic = Medic(
            writer=writer,
            rules=rules,
            sensors=[],
            clock=clock,
            shape=shape,
            suppression=suppression,
            group_cap=group_cap,
        )
        fed = 0
        for t in range(0, seconds + 1, TICK_S):
            clock.advance(start.timestamp() + t - clock.wall())
            while fed < len(pending) and pending[fed][0] <= clock.wall():
                medic.sink.write(pending[fed][1])
                fed += 1
            medic.evaluate()


def _ts(observation: dict[str, Any]) -> float:
    return datetime.fromisoformat(observation["observed_at"]).timestamp()
