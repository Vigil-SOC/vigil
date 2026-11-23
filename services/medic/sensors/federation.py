"""federation_sources (D6-3): per-source state from `/api/federation/sources`.

Read through the gateway (`gateway.get`); `/api/federation/health` is a subset
and isn't read. Emits only the keys a rule reads (epic decision 1):

| Gateway answered                    | Reading                                         |
|-------------------------------------|-------------------------------------------------|
| 200 `{sources: [...], global: {...}}` | per-source values (label `source`), plus `global.enabled` |
| a relayed backend 5xx (store read failed) | `global.enabled` absent, no source rows (a result) |
| 200 with any other body             | error `parse`                                   |
| anything `classify` calls an error  | that error                                      |

`sources.due_at` is `last_success_at` plus that row's `interval_seconds`, so a
rule can judge the source against its own poll interval. Absent when either is
missing, including a source that has never succeeded. `last_error` is never
read: its text can quote a remote system.

`sources.min_severity` is the floor the runner applies, as an ordered gauge:
no floor (null or "") ranks below `low`, then `low`, `medium`, `high`,
`critical`. An unrecognised floor string ranks with `low`, which is the
runner's default. `sources.interval_seconds` and `sources.max_items` are
gauges of the saved settings. `sources.filtered_total` is how many findings
that floor has skipped, a counter on the same epoch as `dropped_total`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value
from services.medic.sensors.gateway import GatewayCall, get

SIGNAL = "federation_sources"
CALLS = (GatewayCall("/api/federation/sources"),)


def _flag(raw: object) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _int(raw: object) -> int | None:
    return raw if type(raw) is int and raw >= 0 else None


def _timestamp(raw: object) -> str | None:
    """The backend's isoformat() time, in the observation timestamp pattern."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    # The columns hold UTC; a naive value only lacks the offset.
    parsed = (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    out = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    if parsed.microsecond:
        out += "." + f"{parsed.microsecond:06d}".rstrip("0")
    return out + "Z"


# dropped_total and filtered_total live on the source's health row: a backend
# restart doesn't reset them, and rows are never deleted. Seeding a row isn't
# a restart either (the synthetic row reads 0 too), so the epoch is fixed. An
# epoch change would read as a backend restart in the upgrade watch.
EPOCH = "federation_sources:db"

# The runner's order (core/federation/runner.py). No floor ranks below low,
# so setting a floor where there was none is a rise. Anything else the runner
# doesn't know is rank 0, the same default it applies.
_FLOOR_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_NO_FLOOR = -1


def _floor(raw: object) -> int | None:
    """The floor as an ordered gauge, or None when it isn't a floor string."""
    if raw is None or raw == "":
        return _NO_FLOOR
    if not isinstance(raw, str):
        return None
    return _FLOOR_RANK.get(raw.lower(), 0)


def _due_at(row: Mapping[str, Any]) -> str | None:
    """When this source's next poll is due: last success plus its interval."""
    success = _timestamp(row.get("last_success_at"))
    interval = _int(row.get("interval_seconds"))
    if success is None or interval is None:
        return None
    due = datetime.fromisoformat(success) + timedelta(seconds=interval)
    return _timestamp(due.isoformat())


def _source(row: Mapping[str, Any], source_id: str) -> list[Value]:
    labels = {"source": source_id}
    return [
        Value.flag("sources.enabled", _flag(row.get("enabled")), labels),
        Value.timestamp(
            "sources.last_success_at", _timestamp(row.get("last_success_at")), labels
        ),
        Value.timestamp("sources.due_at", _due_at(row), labels),
        Value.gauge(
            "sources.consecutive_errors", _int(row.get("consecutive_errors")), labels
        ),
        Value.counter("sources.dropped_total", _int(row.get("dropped_total")), labels),
        Value.counter(
            "sources.filtered_total", _int(row.get("filtered_total")), labels
        ),
        Value.gauge(
            "sources.interval_seconds", _int(row.get("interval_seconds")), labels
        ),
        Value.gauge("sources.max_items", _int(row.get("max_items")), labels),
        Value.gauge("sources.min_severity", _floor(row.get("min_severity")), labels),
        Value.flag("sources.is_configured", _flag(row.get("is_configured")), labels),
    ]


def _parse(body: Any) -> list[Value] | None:
    """None when the body isn't `{sources: [{source_id, ...}], global: {...}}`."""
    if not isinstance(body, dict):
        return None
    sources, settings = body.get("sources"), body.get("global")
    if not isinstance(sources, list) or not isinstance(settings, dict):
        return None
    values: list[Value] = []
    for row in sources:
        if not isinstance(row, dict):
            return None
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return None
        values += _source(row, source_id)
    values.append(Value.flag("global.enabled", _flag(settings.get("enabled"))))
    return values


@dataclass
class FederationSources:
    host: str
    port: int
    id: str = SIGNAL
    service: str = "backend"
    interval_s: int = 60  # epic decision 7
    timeout_s: float = 10.0
    covers: tuple[str, ...] = (SIGNAL,)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        got = await get(
            self.host,
            self.port,
            CALLS[0].path,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [Reading(SIGNAL, "backend", error=got)]
        if got.status >= 500:
            # The backend answered that it couldn't read its store: absent, not off.
            values = [Value.flag("global.enabled", None)]
        elif (parsed := _parse(got.body)) is None:
            return [Reading(SIGNAL, "backend", error=ReadError("parse"))]
        else:
            values = parsed
        return [Reading(SIGNAL, "backend", values=values, epoch=EPOCH)]


def federation_sources(host: str, port: int) -> FederationSources:
    return FederationSources(host, port)
