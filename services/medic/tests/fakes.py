"""A clock the tests drive, so nothing sleeps."""

from __future__ import annotations

import threading


class FakeClock:
    def __init__(self, wall: float = 1_800_000_000.0) -> None:
        self._lock = threading.Lock()
        self._wall = wall
        self._mono = 1_000.0

    def wall(self) -> float:
        with self._lock:
            return self._wall

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._wall += seconds
            self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
