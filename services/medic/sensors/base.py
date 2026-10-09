"""What a sensor is, and what it hands back.

A sensor reads; the framework does everything else. `collect()` returns `Reading`s
(one per signal read). The framework stamps ids, times, target shape and
`sensor_health`, runs the redaction choke point, validates against
`contracts/observation.schema.json` and writes to the sink. See SENSOR_AUTHORING.md.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

# C7 §3: every sensor polls on one of these, fixed (the engine's staleness rule
# is 2 × interval_s, so an interval can't drift).
INTERVALS_S = (30, 60, 300, 21_600)

Shape = Literal["start_sh", "compose", "helm"]
ErrorClass = Literal[
    "timeout",
    "refused",
    "dns",
    "tls",
    "http_status",
    "auth",
    "parse",
    "too_large",
    "redactor_failed",
    "not_installed",
    "other",
]


def _known_signals() -> frozenset[str]:
    doc = json.loads((CONTRACTS / "signal_ids.json").read_text())
    return frozenset(doc["signals"]) | frozenset(doc["log_signals"])


KNOWN_SIGNALS = _known_signals()


class Clock(Protocol):
    def wall(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> Awaitable[None]: ...


@dataclass(frozen=True)
class SensorContext:
    """What a sensor may know about where it runs. Nothing here can write to Vigil."""

    shape: Shape
    vigil_dev_mode: bool = False


@dataclass(frozen=True)
class ReadError:
    cls: ErrorClass
    http_status: int | None = None
    detail: str | None = None  # untrusted; redacted and capped by the framework

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"class": self.cls}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class Value:
    """One typed value; build with the classmethods. Labels, enum values and
    `Reading.instance` are passed raw: the choke point redacts them and then applies
    D3's label rule (`label_safe`) exactly once, so a hash never covers a secret."""

    key: str
    type: Literal["counter", "gauge", "bool", "enum", "timestamp", "text"]
    value: Any
    labels: Mapping[str, str] = field(default_factory=dict)
    trusted: bool = True

    @classmethod
    def _make(
        cls,
        key: str,
        type_: Any,
        value: Any,
        labels: Mapping[str, str] | None,
        trusted: bool,
    ) -> Value:
        return cls(key, type_, value, dict(labels or {}), trusted)

    @classmethod
    def counter(
        cls, key: str, value: int | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "counter", value, labels, True)

    @classmethod
    def gauge(
        cls, key: str, value: float | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "gauge", value, labels, True)

    @classmethod
    def flag(
        cls, key: str, value: bool | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "bool", value, labels, True)

    @classmethod
    def enum(
        cls, key: str, value: str | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "enum", value, labels, True)

    @classmethod
    def timestamp(
        cls, key: str, value: str | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "timestamp", value, labels, True)

    @classmethod
    def text(
        cls, key: str, value: str | None, labels: Mapping[str, str] | None = None
    ) -> Value:
        return cls._make(key, "text", value, labels, False)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "state": "absent" if self.value is None else "present",
            "value": self.value,
            "trust": "trusted" if self.trusted else "untrusted",
        }
        if self.labels:
            out["labels"] = dict(self.labels)
        return out


@dataclass(frozen=True)
class Reading:
    """One read of one signal. `error` set ⇒ the read failed (outcome: error) and
    `values` must be empty. A read that worked and says Vigil is down is not an
    error: return its values (C7 §4)."""

    signal: str
    service: str
    values: Sequence[Value] = ()
    error: ReadError | None = None
    instance: str | None = None
    epoch: str | None = None
    source_ts: str | None = None


@runtime_checkable
class Sensor(Protocol):
    """The plugin interface. Implement it as a plain class with these attributes.

    `id`: `^[a-z][a-z0-9_.-]{0,63}$`, stable across restarts.
    `service`: the service it reads (observation.schema.json `service`).
    `interval_s`: one of INTERVALS_S. `timeout_s`: hard cap per `collect()`, < interval_s.
    `covers`: the signal ids it may emit (contracts/signal_ids.json).
    `uses_vigil_api`: reads Vigil's API (through the gateway); off on a DEV_MODE install (K1 T-37).
    `collect()`: async, no blocking calls; never writes; may raise (the framework
    turns that into a counted failure).
    """

    id: str
    service: str
    interval_s: int
    timeout_s: float
    covers: tuple[str, ...]
    uses_vigil_api: ClassVar[bool] | bool

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]: ...


def check_sensor(sensor: Sensor) -> None:
    """Refuse a sensor that breaks the contract, at registration, not at 3 a.m."""
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", sensor.id):
        raise ValueError(f"sensor id {sensor.id!r} doesn't fit the schema pattern")
    if sensor.interval_s not in INTERVALS_S:
        raise ValueError(
            f"{sensor.id}: interval_s {sensor.interval_s} isn't one of C7's {INTERVALS_S}"
        )
    if not 0 < sensor.timeout_s < sensor.interval_s:
        raise ValueError(f"{sensor.id}: timeout_s must be > 0 and < interval_s")
    if not sensor.covers:
        raise ValueError(f"{sensor.id}: covers no signal")
    unknown = set(sensor.covers) - KNOWN_SIGNALS
    if unknown:
        raise ValueError(
            f"{sensor.id}: signals not in contracts/signal_ids.json: {sorted(unknown)}"
        )
