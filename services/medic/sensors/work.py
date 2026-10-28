"""Evidence of work, through the gateway (D6-4): triage arrivals and probe outcomes.

Only the keys a starter rule reads (epic #2062 decision 1):
- `triage`: `sources.arrivals` per source, a counter whose epoch is the UTC day
  (`arrivals_today_by_source` resets at midnight). Read from `sources` only; the
  intake rows carry free text and are never looked at.
- `probe_findings`: `latest_outcome` per probe, from
  `entity_context.probe.score.outcome`. A probe younger than an hour has no score
  and is skipped (`services/daemon/probes.py`).

A backend 5xx the gateway relayed is a result, `ok: false`: the read failed, which
is not the same as zero arrivals. The gateway's own failures are errors (`get`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from services.medic.sensors.base import ReadError, Reading, SensorContext, Value
from services.medic.sensors.gateway import GatewayCall, GatewayReply, get

TRIAGE_CALL = GatewayCall("/api/triage")
PROBE_LIMIT = 50
PROBE_CALL = GatewayCall("/api/v1/findings", query=("data_source", "limit"))
OUTCOMES = frozenset({"hit", "miss", "silent"})


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _failed(signal: str, status: int) -> Reading:
    return Reading(
        signal,
        "backend",
        values=[Value.flag("ok", False), Value.gauge("status_code", status)],
    )


def _error(signal: str, error: ReadError) -> Reading:
    return Reading(signal, "backend", error=error)


def _arrivals(body: Any) -> list[Value] | None:
    """`sources[]` as counters, or None when it isn't the route's shape."""
    sources = body.get("sources") if isinstance(body, dict) else None
    if not isinstance(sources, list):
        return None
    values = []
    for entry in sources:
        if not isinstance(entry, dict):
            return None
        name, count = entry.get("data_source"), entry.get("arrivals")
        if not isinstance(name, str) or not name:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        values.append(Value.counter("sources.arrivals", count, {"source": name}))
    return values


def _probe_name(finding: dict[str, Any], probe: dict[str, Any]) -> str | None:
    name = probe.get("name")
    if isinstance(name, str) and name:
        return name
    parts = str(finding.get("finding_id") or "").split(":")
    if len(parts) == 3 and parts[0] == "probe" and parts[1]:
        return parts[1]
    return None


def _outcomes(body: Any) -> list[Value] | None:
    """Newest scored outcome per probe, or None on a shape or outcome it can't read.

    The list is newest first (the route's default sort), so the first scored
    finding per name wins.
    """
    findings = body.get("findings") if isinstance(body, dict) else None
    if not isinstance(findings, list):
        return None
    latest: dict[str, str] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            return None
        context = finding.get("entity_context")
        probe = context.get("probe") if isinstance(context, dict) else None
        score = probe.get("score") if isinstance(probe, dict) else None
        if not isinstance(score, dict):
            continue  # not scored yet: no value, not "pending"
        outcome = score.get("outcome")
        if outcome not in OUTCOMES:
            return None
        name = _probe_name(finding, probe)
        if name is None:
            return None
        latest.setdefault(name, outcome)
    return [
        Value.enum("latest_outcome", outcome, {"probe": name})
        for name, outcome in sorted(latest.items())
    ]


@dataclass
class TriageArrivals:
    host: str
    port: int
    id: str = "gateway.triage"
    service: str = "backend"
    interval_s: int = 60
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("triage",)
    uses_vigil_api: bool = True
    today: Callable[[], date] = field(default=_utc_today, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        day = (
            self.today()
        )  # before the read: a count from before midnight keeps its day
        got = await get(
            self.host,
            self.port,
            TRIAGE_CALL.path,
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [_error("triage", got)]
        if got.status != 200:
            return [_failed("triage", got.status)]
        values = _arrivals(got.body)
        if values is None:
            return [_error("triage", ReadError("parse"))]
        return [
            Reading(
                "triage",
                "backend",
                values=[Value.flag("ok", True), *values],
                epoch=f"utc-day:{day.isoformat()}",
            )
        ]


@dataclass
class ProbeFindings:
    host: str
    port: int
    id: str = "gateway.probe_findings"
    service: str = "backend"
    interval_s: int = 300
    timeout_s: float = 10.0
    covers: tuple[str, ...] = ("probe_findings",)
    uses_vigil_api: bool = True
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]:
        got: GatewayReply | ReadError = await get(
            self.host,
            self.port,
            PROBE_CALL.path,
            params={"data_source": "probe", "limit": str(PROBE_LIMIT)},
            timeout=self.timeout_s * 0.8,
            transport=self.transport,
        )
        if isinstance(got, ReadError):
            return [_error("probe_findings", got)]
        if got.status != 200:
            return [_failed("probe_findings", got.status)]
        values = _outcomes(got.body)
        if values is None:
            return [_error("probe_findings", ReadError("parse"))]
        return [
            Reading(
                "probe_findings", "backend", values=[Value.flag("ok", True), *values]
            )
        ]


def work_sensors(host: str, port: int) -> list[TriageArrivals | ProbeFindings]:
    return [TriageArrivals(host, port), ProbeFindings(host, port)]
