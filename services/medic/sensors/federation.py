"""federation_sources (D6-3): per-source state from `/api/federation/sources`.

Read through the gateway (`gateway.get`); `/api/federation/health` is a subset
and isn't read. Emits only the keys a rule reads (epic decision 1):

| Gateway answered                    | Reading                                         |
|-------------------------------------|-------------------------------------------------|
| 200 `{sources: [...], global: {...}}` | per-source values (label `source`), plus `global.enabled` |
| a relayed backend 5xx (store read failed) | `global.enabled` absent, no source rows (a result) |
| 200 with any other body             | error `parse`                                   |
| anything `classify` calls an error  | that error                                      |

`last_error` is never read: its text can quote a remote system.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


# dropped_total is a column on the source's DB row: a backend restart doesn't
# reset it, and rows are never deleted. Seeding a row isn't a restart either
# (the synthetic row reads 0 too), so the epoch is fixed. An epoch change would
# read as a backend restart in the upgrade watch.
EPOCH = "federation_sources:db"


def _source(row: Mapping[str, Any], source_id: str) -> list[Value]:
    labels = {"source": source_id}
    return [
        Value.flag("sources.enabled", _flag(row.get("enabled")), labels),
        Value.timestamp(
            "sources.last_success_at", _timestamp(row.get("last_success_at")), labels
        ),
        Value.gauge(
            "sources.consecutive_errors", _int(row.get("consecutive_errors")), labels
        ),
        Value.counter("sources.dropped_total", _int(row.get("dropped_total")), labels),
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
