"""Cost forecast for the Day 1 Report.

Arrivals come from imported history. Saved limits come from
``orchestrator.settings``, and measured spend from the agent layer's spend
events. One part failing leaves the others, and the rest of the report, to
finish. Nothing here is sent to a model.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.agents.projections import (
    PROJECTION_READ_CONCURRENCY,
    read_spend,
    run_id_for,
)
from core.config import get_settings
from core.integrations.health import LEGACY_POLLED
from core.integrations.health import collect as collect_health
from core.integrations.readiness import NOT_READY, READY, UNAVAILABLE, UNKNOWN
from core.memory.imported_history import RUNNING, list_imports
from core.storage.models import Finding, HistoryTicket, Investigation, SystemConfig
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

WINDOW_DAYS = 90
SETTINGS_KEY = "orchestrator.settings"
FIX = "Import alert and ticket history, then run the Day 1 Report again."
SCREEN = "/settings?section=data&tab=history"
# A read that 404s proves the spend route is up when this install has no investigations.
_PROBE = "00000000-0000-0000-0000-000000000000"
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
# OrchestratorSettingsConfig's defaults and bounds (services/api/routers/config.py).
# core does not import that router. A saved value outside these falls back.
_DEFAULTS = {
    "enabled": False,
    "max_concurrent_agents": 3,
    "max_runtime_per_investigation": 3600,
    "max_cost_per_investigation": 5.0,
    "max_total_hourly_cost": 20.0,
}
_BOUNDS = {
    "max_concurrent_agents": (1, 10),
    "max_runtime_per_investigation": (60, 86400),
    "max_cost_per_investigation": (0.5, 100.0),
    "max_total_hourly_cost": (1.0, 500.0),
}

Service = Literal["running", "stopped", "unavailable", "unknown", "not_serving"]
FindingsState = Literal["measured", "not_connected", "not_checked"]


class Day1ForecastSource(BaseModel):
    source: str
    window_start: str
    window_end: str
    opened: int
    opened_missing: int
    outside: int
    week: float
    findings: Optional[int] = None
    findings_status: FindingsState
    findings_reason: Optional[str] = None


class Day1ForecastArrivals(BaseModel):
    status: Literal["ok", "not_checked"]
    reason: Optional[str] = None
    fix: Optional[str] = None
    screen: Optional[str] = None
    sources: list[Day1ForecastSource] = Field(default_factory=list)
    busiest_hour: Optional[int] = None
    busiest_at: Optional[str] = None


class Day1ForecastLimits(BaseModel):
    status: Literal["ok", "not_checked", "omitted"]
    reason: Optional[str] = None
    service: Service
    enabled: Optional[bool] = None
    concurrent: Optional[int] = None
    minutes: Optional[float] = None
    per_investigation: Optional[float] = None
    hourly: Optional[float] = None
    covers: Optional[float] = None
    triage: Optional[bool] = None


class Day1ForecastSpend(BaseModel):
    status: Literal["ok", "not_checked", "omitted"]
    reason: Optional[str] = None
    wholly_priced: int = 0
    low: Optional[float] = None
    high: Optional[float] = None
    median: Optional[float] = None
    not_counted: int = 0
    unpriced_model: int = 0


class Day1Forecast(BaseModel):
    arrivals: Day1ForecastArrivals
    limits: Day1ForecastLimits
    spend: Day1ForecastSpend


def produce(state: dict) -> dict[str, Any]:
    """The forecast body stored at ``evidence["history.forecast"]``."""
    service = _service(state)
    arrivals = _arrivals()
    if service in ("stopped", "unavailable"):
        limits = Day1ForecastLimits(status="omitted", service=service)
        spend = Day1ForecastSpend(status="omitted")
    else:
        limits = _limits(service)
        spend = _spend()
    return Day1Forecast(arrivals=arrivals, limits=limits, spend=spend).model_dump(
        mode="json"
    )


def failed(reason: str) -> dict[str, Any]:
    """Every part not checked. Used when the forecast itself could not be built."""
    body = Day1Forecast(
        arrivals=Day1ForecastArrivals(status="not_checked", reason=reason),
        limits=Day1ForecastLimits(
            status="not_checked", service="unknown", reason=reason
        ),
        spend=Day1ForecastSpend(status="not_checked", reason=reason),
    )
    return body.model_dump(mode="json")


def _service(state: dict) -> Service:
    name = state.get("state")
    if name == UNAVAILABLE:
        return "unavailable"
    if name == UNKNOWN:
        return "unknown"
    daemon = next(
        (item for item in state.get("items") or [] if item.get("id") == "daemon"),
        None,
    )
    if daemon is None:
        return "running" if name in (READY, NOT_READY) else "unknown"
    if "stopped" in str(daemon.get("text") or "").lower():
        return "stopped"
    return "not_serving"


def _arrivals() -> Day1ForecastArrivals:
    try:
        with unit_of_work() as session:
            running = _running_since(session)
            if running is not None:
                return Day1ForecastArrivals(
                    status="not_checked",
                    reason=f"not checked: import running since {_stamp(running)}.",
                    fix=FIX,
                    screen=SCREEN,
                )
            rows = session.query(
                HistoryTicket.source_system,
                HistoryTicket.ticket_id,
                HistoryTicket.opened_at,
                HistoryTicket.closed_at,
                HistoryTicket.opened_missing,
            ).all()
    except (
        Exception
    ) as exc:  # noqa: BLE001 - this part is not checked; the rest finishes
        logger.error(
            "Day 1 Report could not read imported history: %s", type(exc).__name__
        )
        return Day1ForecastArrivals(
            status="not_checked",
            reason="not checked: could not read imported history.",
        )
    kept = _dedupe(rows)
    if not kept:
        return Day1ForecastArrivals(
            status="not_checked",
            reason="not checked: no history imported",
            fix=FIX,
            screen=SCREEN,
        )
    try:
        connected = _connected()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Day 1 Report could not read connected sources: %s", type(exc).__name__
        )
        connected = None
    grouped: dict[str, list] = {}
    for row in kept:
        grouped.setdefault(row.source_system, []).append(row)
    sources: list[Day1ForecastSource] = []
    openings: list[datetime] = []
    for name in sorted(grouped):
        source, found = _one_source(name, grouped[name], connected)
        sources.append(source)
        openings.extend(found)
    hour, at = _busiest(openings)
    return Day1ForecastArrivals(
        status="ok",
        sources=sources,
        busiest_hour=hour,
        busiest_at=at,
    )


def _running_since(session: Session) -> Optional[datetime]:
    """The start of an import whose lock is held. A dead ``running`` row is not one."""
    running = [
        row["started_at"]
        for row in list_imports(session)
        if row.get("status") == RUNNING and row.get("started_at") is not None
    ]
    if not running:
        return None
    return min(_as_utc(moment) for moment in running)


def _dedupe(rows: list) -> list:
    """One ticket per source and ticket id. A row with no id is never merged."""
    ordered = sorted(rows, key=lambda row: _as_utc(row.closed_at), reverse=True)
    seen: set[tuple[str, str]] = set()
    kept = []
    for row in ordered:
        ticket_id = row.ticket_id
        if ticket_id:
            key = (row.source_system, ticket_id)
            if key in seen:
                continue
            seen.add(key)
        kept.append(row)
    return kept


def _one_source(
    name: str, rows: list, connected: Optional[set[str]]
) -> tuple[Day1ForecastSource, list[datetime]]:
    closes = [_as_utc(row.closed_at) for row in rows]
    anchor = max(closes)
    start = anchor - timedelta(days=WINDOW_DAYS)
    missing = 0
    outside = 0
    openings: list[datetime] = []
    for row in rows:
        closed = _as_utc(row.closed_at)
        if closed < start:
            outside += 1
            continue
        if row.opened_missing or row.opened_at is None:
            missing += 1
            continue
        opened = _as_utc(row.opened_at)
        if opened < start or opened > anchor:
            continue
        openings.append(opened)
    findings, findings_status, findings_reason = _measured(
        name, start, anchor, connected
    )
    source = Day1ForecastSource(
        source=name,
        window_start=_iso(start),
        window_end=_iso(anchor),
        opened=len(openings),
        opened_missing=missing,
        outside=outside,
        week=_week(openings, start, anchor),
        findings=findings,
        findings_status=findings_status,
        findings_reason=findings_reason,
    )
    return source, openings


def _week(openings: list[datetime], start: datetime, end: datetime) -> float:
    """Median calendar day's openings, zeros included, times 7. Estimated."""
    first, last = start.date(), end.date()
    span = (last - first).days + 1
    if span <= 0:
        return 0.0
    counts = [0] * span
    for opened in openings:
        day = opened.date()
        if first <= day <= last:
            counts[(day - first).days] += 1
    return float(median(counts) * 7)


def _busiest(openings: list[datetime]) -> tuple[int, Optional[str]]:
    """Install-wide openings in one UTC hour. Ties take the earlier hour."""
    if not openings:
        return 0, None
    counts: dict[datetime, int] = {}
    for opened in openings:
        hour = opened.replace(minute=0, second=0, microsecond=0)
        counts[hour] = counts.get(hour, 0) + 1
    moment = min(counts, key=lambda hour: (-counts[hour], hour))
    return counts[moment], _iso(moment)


def _connected() -> set[str]:
    """Source strings a switched-on integration shares with history and findings.

    Health records the integration id (``microsoft-defender``). Tickets and
    findings use the adapter name (``microsoft_defender``).
    """
    names: set[str] = set()
    for row in collect_health():
        source_id = row.get("source_id")
        if not row.get("switched_on") or not source_id:
            continue
        names.add(source_id)
        mapped = LEGACY_POLLED.get(source_id)
        if mapped:
            names.add(mapped)
    return names


def _measured(
    name: str,
    start: datetime,
    end: datetime,
    connected: Optional[set[str]],
) -> tuple[Optional[int], FindingsState, Optional[str]]:
    if connected is None:
        return None, "not_checked", "Could not read which sources are connected."
    if name not in connected:
        return None, "not_connected", None
    try:
        with unit_of_work() as session:
            count = (
                session.query(func.count(Finding.finding_id))
                .filter(
                    Finding.data_source == name,
                    Finding.timestamp.isnot(None),
                    Finding.timestamp >= _naive(start),
                    Finding.timestamp <= _naive(end),
                )
                .scalar()
            )
    except Exception as exc:  # noqa: BLE001 - history figures stay
        logger.error(
            "Day 1 Report could not read findings for %s: %s",
            name,
            type(exc).__name__,
        )
        return None, "not_checked", "Could not read findings stored on this install."
    return int(count or 0), "measured", None


def _limits(service: Service) -> Day1ForecastLimits:
    try:
        saved = _saved()
    except Exception as exc:  # noqa: BLE001
        logger.error("Day 1 Report could not read saved limits: %s", type(exc).__name__)
        return Day1ForecastLimits(
            status="not_checked",
            service=service,
            reason="not checked: could not read the saved limits.",
        )
    triage: Optional[bool]
    try:
        triage = bool(get_settings().daemon_auto_triage)
    except Exception as exc:  # noqa: BLE001
        logger.error("Day 1 Report could not read triage: %s", type(exc).__name__)
        triage = None
    per = float(saved["max_cost_per_investigation"])
    hourly = float(saved["max_total_hourly_cost"])
    return Day1ForecastLimits(
        status="ok",
        service=service,
        enabled=bool(saved["enabled"]),
        concurrent=int(saved["max_concurrent_agents"]),
        minutes=int(saved["max_runtime_per_investigation"]) / 60,
        per_investigation=per,
        hourly=hourly,
        covers=(hourly / per) if per > 0 else None,
        triage=triage,
    )


def _saved() -> dict[str, Any]:
    """The saved row, each field falling back to OrchestratorSettingsConfig."""
    with unit_of_work() as session:
        row = session.get(SystemConfig, SETTINGS_KEY)
        saved = row.value if row is not None and isinstance(row.value, dict) else {}
    merged = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        if key not in saved or saved[key] is None:
            continue
        merged[key] = _coerce(key, saved[key], default)
    return merged


def _coerce(key: str, value: Any, default: Any) -> Any:
    if key == "enabled":
        return value if isinstance(value, bool) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    low, high = _BOUNDS[key]
    if key == "max_cost_per_investigation" or key == "max_total_hourly_cost":
        number = float(value)
    elif isinstance(value, float) and not value.is_integer():
        return default
    else:
        number = int(value)
    if number < low or number > high:
        return default
    return number


def _spend() -> Day1ForecastSpend:
    try:
        ids = _investigation_ids()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Day 1 Report could not list investigations: %s", type(exc).__name__
        )
        return Day1ForecastSpend(
            status="not_checked",
            reason="not checked: could not read investigations.",
        )
    try:
        results = asyncio.run(_gather(ids))
    except Exception as exc:  # noqa: BLE001
        logger.error("Day 1 Report could not read spend: %s", type(exc).__name__)
        return Day1ForecastSpend(status="not_checked", reason=_spend_reason(exc))
    if results and all(isinstance(item, Exception) for item in results):
        first = next(item for item in results if isinstance(item, Exception))
        return Day1ForecastSpend(status="not_checked", reason=_spend_reason(first))
    costs: list[float] = []
    not_counted = 0
    unpriced_model = 0
    for item in results:
        if isinstance(item, Exception) or item is None:
            not_counted += 1
            continue
        calls = int(item["calls"])
        unpriced = int(item["unpriced_calls"])
        if calls > 0 and unpriced == 0:
            costs.append(float(item["cost_usd"]))
            continue
        not_counted += 1
        if calls > 0 and unpriced == calls:
            unpriced_model += 1
    low = high = middle = None
    if len(costs) >= 20:
        middle = _money(float(median(costs)))
    elif costs:
        low, high = _money(min(costs)), _money(max(costs))
    return Day1ForecastSpend(
        status="ok",
        wholly_priced=len(costs),
        low=low,
        high=high,
        median=middle,
        not_counted=not_counted,
        unpriced_model=unpriced_model,
    )


def _investigation_ids() -> list[str]:
    cutoff = utcnow() - timedelta(days=WINDOW_DAYS)
    with unit_of_work() as session:
        rows = (
            session.query(Investigation.investigation_id)
            .filter(Investigation.created_at >= cutoff)
            .all()
        )
    return [row[0] for row in rows]


async def _gather(ids: list[str]) -> list[Any]:
    if not ids:
        await read_spend(_PROBE)
        return []
    gate = asyncio.Semaphore(PROJECTION_READ_CONCURRENCY)
    lost = asyncio.Event()

    async def one(investigation_id: str) -> Any:
        if lost.is_set():
            return RuntimeError("could not reach the agent layer")
        async with gate:
            if lost.is_set():
                return RuntimeError("could not reach the agent layer")
            try:
                return await read_spend(run_id_for(investigation_id))
            except RuntimeError as exc:
                if str(exc).startswith("could not reach"):
                    lost.set()
                return exc
            except Exception as exc:  # noqa: BLE001 - one read, counted below
                return exc

    return list(await asyncio.gather(*(one(item) for item in ids)))


def _spend_reason(exc: BaseException) -> str:
    message = str(exc)
    if message.startswith("the agent layer answered"):
        return f"not checked: {message}."
    if "AGENT_INTERNAL_TOKEN" in message:
        return "not checked: the agent layer is not configured."
    return "not checked: could not reach the agent layer."


def _money(value: float) -> float:
    return float(f"{value:.4f}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return _naive(value).isoformat() + "Z"


def _stamp(value: datetime) -> str:
    moment = _as_utc(value)
    return (
        f"{moment.day} {_MONTHS[moment.month - 1]} "
        f"{moment.hour:02d}:{moment.minute:02d} UTC"
    )
