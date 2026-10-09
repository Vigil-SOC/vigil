"""Where finished observations go. S4 implements `Sink` with the engine and store."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class Sink(Protocol):
    def write(self, observation: Mapping[str, Any]) -> None:
        """Take one redacted, schema-valid observation. May raise; the caller decides."""
        ...


class MemorySink:
    """For tests (Medic's and the factory's sensor tests)."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []

    def write(self, observation: Mapping[str, Any]) -> None:
        self.observations.append(dict(observation))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [o for o in self.observations if o["kind"] == kind]
