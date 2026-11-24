"""integration_health (#5072): OpenCTI's facts from `/api/integration-health`.

Read through the gateway (`gateway.get`). The backend collects the facts
(`services/api/opencti_facts.py`, #5071) onto the `opencti` source's record;
Medic never calls OpenCTI. Emits only the keys a rule reads (epic decision 1):

| Gateway answered                            | Reading                                  |
|---------------------------------------------|------------------------------------------|
| 200 `{sources: [...], ...}`                 | the three `opencti.*` values below       |
| a relayed backend 5xx (store read failed)   | the three values absent (a result)       |
| 200 with any other body                     | error `parse`                            |
| anything `classify` calls an error          | that error                               |

`opencti.newest_intel_at` is the `created_at` of OpenCTI's newest object,
`opencti.queue_messages_total` the messages waiting across its connectors, and
`opencti.collected_at` when the backend last read them. Each is absent while
OpenCTI isn't a source, its record has no facts (never read, or reset by
Settings), or the token couldn't read that fact: absent, never zero.
`connectors`, `missing` and `version` are never read; connector names are
operator text from a remote system.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value
from services.medic.sensors.federation import _int, _timestamp
from services.medic.sensors.gateway import GatewayCall, get

SIGNAL = "integration_health"
CALLS = (GatewayCall("/api/integration-health"),)
SOURCE = "opencti"


def _values(facts: Mapping[str, Any]) -> list[Value]:
    return [
        Value.timestamp(
            "opencti.newest_intel_at", _timestamp(facts.get("newest_intel_at"))
        ),
        Value.gauge(
            "opencti.queue_messages_total", _int(facts.get("queue_messages_total"))
        ),
        Value.timestamp("opencti.collected_at", _timestamp(facts.get("collected_at"))),
    ]


def _parse(body: Any) -> list[Value] | None:
    """None when the body isn't `{sources: [{source_id, ...}], ...}`."""
    if not isinstance(body, dict) or not isinstance(body.get("sources"), list):
        return None
    facts: Mapping[str, Any] = {}
    for row in body["sources"]:
        if not isinstance(row, dict) or not isinstance(row.get("source_id"), str):
            return None
        if row["source_id"] == SOURCE:
            record = row.get("record")
            got = record.get("facts") if isinstance(record, dict) else None
            facts = got if isinstance(got, dict) else {}
    return _values(facts)


@dataclass
class IntegrationHealth:
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
            # The backend answered that it couldn't read its store: absent, not zero.
            values = _values({})
        elif (parsed := _parse(got.body)) is None:
            return [Reading(SIGNAL, "backend", error=ReadError("parse"))]
        else:
            values = parsed
        return [Reading(SIGNAL, "backend", values=values)]


def integration_health(host: str, port: int) -> IntegrationHealth:
    return IntegrationHealth(host, port)
