"""The bounded queue between sensors and the choke point, with counted drops.

Memory is fixed by `capacity`, not by how fast sensors produce (C7 §5). When it's
full the oldest observation goes (the engine reads the newest), and the drop is
counted against the sensor it came from; that count rides on the sensor's next
`sensor_health` (`counters.dropped`, state `degraded`). `sensor_health` itself is
never dropped: it's coalesced to the newest per sensor, so it's bounded by the
number of sensors.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

# PROVISIONAL (S3-2). Non-log observations only: at C7's intervals the whole v1
# sensor set produces well under 100 per 15 s tick, so this is minutes of backlog.
# Log lines get their own 10,000-line buffer in D6 (C7 §5).
DEFAULT_CAPACITY = 1024


@dataclass
class SensorStats:
    """Counters since this run started (observation.schema.json health.counters)."""

    reads: int = 0
    failures: int = 0
    dropped: int = 0
    redactor_failures: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "reads": self.reads,
            "failures": self.failures,
            "dropped": self.dropped,
            "redactor_failures": self.redactor_failures,
        }


class Stats(defaultdict[str, SensorStats]):
    def __init__(self) -> None:
        super().__init__(SensorStats)

    def total(self, field: str) -> int:
        return sum(getattr(s, field) for s in self.values())


class Bus:
    def __init__(self, stats: Stats, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.stats = stats
        self._items: deque[dict[str, Any]] = deque()
        self._health: dict[str, dict[str, Any]] = {}

    def put(self, draft: dict[str, Any]) -> None:
        if len(self._items) >= self.capacity:
            oldest = self._items.popleft()
            self.stats[oldest["_subject"]].dropped += 1
        self._items.append(draft)

    def put_health(self, draft: dict[str, Any]) -> None:
        self._health[draft["health"]["sensor"]] = draft

    def take(self) -> list[dict[str, Any]]:
        out = [*self._items, *self._health.values()]
        self._items.clear()
        self._health.clear()
        return out

    def __len__(self) -> int:
        return len(self._items) + len(self._health)
