"""Spend since Production Start, beside the latest Day 1 forecast.

Investigations, hunts and chat are read one run at a time from the spend
route. Dollars come only from wholly priced runs. Triage and insight are
not recorded. Day 1's build does not call this.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.agents.projections import (
    PROJECTION_READ_CONCURRENCY,
    read_spend,
)
from core.agents.projections import run_id_for as investigation_run_id
from core.chat.conversation_service import ledger_key
from core.detections.inventory.accurate import MIN_JUDGED
from core.llm.chat_layers import run_id_for as chat_run_id
from core.reporting.analytics_service import _wilson
from core.reporting.day1 import _iso, _parse
from core.storage.models import Conversation, Investigation, ReportRecord, WorkflowRun
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# A 404 proves the spend route is up when the window holds no runs.
_PROBE = "00000000-0000-0000-0000-000000000000"
_NO_WINDOW = "not checked: the production window has no start."
_READ_FAILED = "not checked: could not read runs in the window."
_NO_DAY1 = "not checked: no Day 1 Report"
_TRIAGE = "Triage and insight are not recorded."
_SCOPE = "the whole install"
_BOUND = "none in this window"
_WHOLLY = "a run with at least one call and no unpriced call"
_DOLLARS = "sum of wholly priced runs; an unpriced run has no dollar figure"
_CALLS = "calls on wholly priced runs"
_TOKENS = "input, output, cache read and cache write on wholly priced runs"
_WALL = (
    "completed_at minus started_at for an investigation, duration_ms for a hunt;"
    " chat is not recorded"
)
_LEFT_OUT = "created before Production Start; the spend read returns the whole run"
# The beside-line names these three. A recurring Day 1 run is not one of them.
_DAY1 = ("readiness", "fallback", "on_demand")
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


class _Run(NamedTuple):
    kind: str
    name: Optional[str]
    run_id: Optional[str]
    wall_ms: Optional[int]


def spend(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """The body stored at ``evidence["production.spend"]``."""
    start = _parse(started_at)
    end = _parse(window_end)
    if start is None or end is None:
        return {"not_checked": _NO_WINDOW, "triage_insight": _TRIAGE}
    try:
        runs, chat_ids, before, forecast = _load(start, end)
    except Exception as exc:  # noqa: BLE001 - this module only; the report continues
        logger.error("production spend could not read runs: %s", type(exc).__name__)
        return {"not_checked": _READ_FAILED, "triage_insight": _TRIAGE}
    runs.extend(_chat_runs(chat_ids))
    try:
        results = asyncio.run(_gather([run.run_id for run in runs if run.run_id]))
    except Exception as exc:  # noqa: BLE001 - the agent layer, not the other modules
        logger.error("production spend could not read spend: %s", type(exc).__name__)
        return {
            "not_checked": _reason(exc),
            "triage_insight": _TRIAGE,
            "forecast": forecast,
        }
    failed = _failed(results)
    if failed is not None:
        return {"not_checked": failed, "triage_insight": _TRIAGE, "forecast": forecast}
    try:
        return _body(runs, results, before, forecast, start, end)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - a bad row is not checked; the report continues
        logger.error("production spend could not state figures: %s", type(exc).__name__)
        return {
            "not_checked": _READ_FAILED,
            "triage_insight": _TRIAGE,
            "forecast": forecast,
        }


def _load(
    start: datetime, end: datetime
) -> tuple[list[_Run], list[str], dict[str, int], dict[str, Any]]:
    with unit_of_work() as session:
        investigations = (
            session.query(
                Investigation.investigation_id,
                Investigation.started_at,
                Investigation.completed_at,
            )
            .filter(
                Investigation.created_at >= start,
                Investigation.created_at <= end,
            )
            .all()
        )
        hunts = (
            session.query(
                WorkflowRun.run_id,
                WorkflowRun.workflow_name,
                WorkflowRun.duration_ms,
            )
            .filter(
                WorkflowRun.started_at >= start,
                WorkflowRun.started_at <= end,
                WorkflowRun.trigger_context["run_kind"].astext == "hunt",
            )
            .all()
        )
        chat_ids = [
            row[0]
            for row in session.query(Conversation.id)
            .filter(
                Conversation.created_at >= start,
                Conversation.created_at <= end,
            )
            .all()
        ]
        before = {
            "investigations": _count_before(session, Investigation, start),
            "hunts": int(
                session.query(func.count(WorkflowRun.run_id))
                .filter(
                    WorkflowRun.started_at < start,
                    WorkflowRun.trigger_context["run_kind"].astext == "hunt",
                )
                .scalar()
                or 0
            ),
            "chat": _count_before(session, Conversation, start),
        }
        forecast = _forecast(session, len(investigations), start, end)
    runs = [
        _Run(
            "investigation",
            row.investigation_id,
            investigation_run_id(row.investigation_id),
            _wall(row.started_at, row.completed_at),
        )
        for row in investigations
    ]
    runs.extend(
        _Run(
            "hunt",
            _hunt_name(row.workflow_name),
            row.run_id,
            _duration(row.duration_ms),
        )
        for row in hunts
    )
    return runs, chat_ids, before, forecast


def _count_before(session: Session, model: Any, start: datetime) -> int:
    return int(
        session.query(func.count())
        .select_from(model)
        .filter(model.created_at < start)
        .scalar()
        or 0
    )


def _chat_runs(conversation_ids: list[str]) -> list[_Run]:
    """Address each chat before the gather. The id is not kept on the run."""
    runs: list[_Run] = []
    for conversation_id in conversation_ids:
        try:
            run_id = chat_run_id(ledger_key(conversation_id))
        except Exception as exc:  # noqa: BLE001 - one chat, counted as not read
            logger.error(
                "production spend could not address a chat run: %s",
                type(exc).__name__,
            )
            runs.append(_Run("chat", None, None, None))
            continue
        runs.append(_Run("chat", None, run_id, None))
    return runs


def _hunt_name(name: Optional[str]) -> str:
    text = (name or "").strip()
    return text or "Hunt"


def _wall(started: Optional[datetime], completed: Optional[datetime]) -> Optional[int]:
    if started is None or completed is None:
        return None
    ms = int(round((completed - started).total_seconds() * 1000))
    return ms if ms >= 0 else None


def _duration(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


async def _gather(run_ids: list[str]) -> list[Any]:
    if not run_ids:
        await read_spend(_PROBE)
        return []
    gate = asyncio.Semaphore(PROJECTION_READ_CONCURRENCY)
    lost = asyncio.Event()

    async def one(run_id: str) -> Any:
        if lost.is_set():
            return RuntimeError("could not reach the agent layer")
        async with gate:
            if lost.is_set():
                return RuntimeError("could not reach the agent layer")
            try:
                return await read_spend(run_id)
            except RuntimeError as exc:
                if str(exc).startswith("could not reach"):
                    lost.set()
                return exc
            except Exception as exc:  # noqa: BLE001 - one read, counted below
                return exc

    return list(await asyncio.gather(*(one(run_id) for run_id in run_ids)))


def _failed(results: list[Any]) -> Optional[str]:
    errors = [item for item in results if isinstance(item, Exception)]
    unreachable = [
        item
        for item in errors
        if str(item).startswith("could not reach")
        or "AGENT_INTERNAL_TOKEN" in str(item)
    ]
    if unreachable:
        return _reason(unreachable[0])
    if results and len(errors) == len(results):
        return _reason(errors[0])
    return None


def _reason(exc: BaseException) -> str:
    message = str(exc)
    if "AGENT_INTERNAL_TOKEN" in message:
        return "not checked: the agent layer is not configured."
    if message.startswith("the agent layer answered"):
        return f"not checked: {message}."
    return "not checked: could not reach the agent layer."


def _body(
    runs: list[_Run],
    results: list[Any],
    before: dict[str, int],
    forecast: dict[str, Any],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    pending = iter(results)
    priced: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    no_calls = 0
    unread = 0
    for run in runs:
        if run.run_id is None:
            unread += 1
            continue
        item = next(pending)
        if isinstance(item, Exception) or item is None:
            if item is None:
                no_calls += 1
            else:
                unread += 1
            continue
        row = _row(run, item)
        calls = row["calls"]
        unpriced_calls = int(item["unpriced_calls"])
        if calls > 0 and unpriced_calls == 0:
            row["cost_usd"] = _money(float(item["cost_usd"]))
            priced.append(row)
        elif calls > 0 and unpriced_calls >= calls:
            unpriced.append(row)
        elif calls > 0:
            row["unpriced_calls"] = unpriced_calls
            partial.append(row)
        else:
            no_calls += 1
    span = _span(start, end)
    # A 404 and a failed read are not counted, so they are not in the rate.
    counted = len(priced) + len(unpriced) + len(partial)
    share = _share(len(priced), counted if runs else 0)
    if runs and counted == 0:
        share["bound"] = "no recorded calls"
    return {
        "scope": _SCOPE,
        "triage_insight": _TRIAGE,
        **span,
        "share": {
            "label": "Wholly priced",
            "definition": _WHOLLY,
            **share,
            **span,
        },
        "priced": priced,
        "unpriced": unpriced,
        "partial": partial,
        "no_calls": no_calls,
        "unread": unread,
        "left_out": {**before, "definition": _LEFT_OUT},
        "totals": _totals(priced),
        "definitions": {
            "dollars": _DOLLARS,
            "calls": _CALLS,
            "tokens": _TOKENS,
            "wall": _WALL,
        },
        "forecast": forecast,
    }


def _row(run: _Run, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": run.kind,
        "name": None if run.kind == "chat" else run.name,
        "calls": int(item["calls"]),
        "input": int(item["input"]),
        "output": int(item["output"]),
        "cache_read": int(item["cache_read"]),
        "cache_write": int(item["cache_write"]),
        "wall_ms": run.wall_ms,
    }


def _totals(priced: list[dict[str, Any]]) -> dict[str, Any]:
    walls = [row["wall_ms"] for row in priced if isinstance(row.get("wall_ms"), int)]
    if not priced:
        return {
            "calls": 0,
            "cost_usd": None,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "wall_ms": None,
            "wall_recorded": 0,
            "wall_unrecorded": 0,
        }
    return {
        "calls": sum(int(row["calls"]) for row in priced),
        "cost_usd": _money(sum(float(row["cost_usd"]) for row in priced)),
        "input": sum(int(row["input"]) for row in priced),
        "output": sum(int(row["output"]) for row in priced),
        "cache_read": sum(int(row["cache_read"]) for row in priced),
        "cache_write": sum(int(row["cache_write"]) for row in priced),
        "wall_ms": sum(walls) if walls else None,
        "wall_recorded": len(walls),
        "wall_unrecorded": len(priced) - len(walls),
    }


def _forecast(
    session: Session, investigations: int, start: datetime, end: datetime
) -> dict[str, Any]:
    record = (
        session.query(ReportRecord)
        .filter(ReportRecord.trigger.in_(_DAY1))
        .order_by(ReportRecord.recorded_at.desc())
        .first()
    )
    if record is None:
        return {"not_checked": _NO_DAY1}
    evidence = record.evidence if isinstance(record.evidence, dict) else {}
    stored = evidence.get("history.forecast")
    if not isinstance(stored, dict):
        return {
            "not_checked": (
                "not checked: the Day 1 Report of "
                f"{_day(record.recorded_at)} has no history sections"
            )
        }
    arrivals = (
        stored.get("arrivals") if isinstance(stored.get("arrivals"), dict) else {}
    )
    limits = stored.get("limits") if isinstance(stored.get("limits"), dict) else {}
    sources = []
    for source in arrivals.get("sources") or []:
        if not isinstance(source, dict) or not isinstance(source.get("source"), str):
            continue
        week = source.get("week")
        if not _number(week):
            continue
        sources.append({"source": source["source"], "week": float(week)})
    stated = limits.get("status") == "ok"
    covers = limits.get("covers")
    per = limits.get("per_investigation")
    hour = arrivals.get("busiest_hour")
    reason = arrivals.get("reason")
    return {
        "run_id": record.run_id,
        "recorded_at": _iso(record.recorded_at),
        "sources": sources,
        "arrivals_reason": (
            reason
            if arrivals.get("status") == "not_checked" and isinstance(reason, str)
            else None
        ),
        "busiest_hour": (
            hour if isinstance(hour, int) and not isinstance(hour, bool) else None
        ),
        "busiest_at": (
            arrivals.get("busiest_at")
            if isinstance(arrivals.get("busiest_at"), str)
            else None
        ),
        "investigations": investigations,
        **_span(start, end),
        "limits_stated": stated,
        "covers": float(covers) if stated and _number(covers) else None,
        "per_investigation": float(per) if stated and _number(per) else None,
    }


def _share(k: int, n: int) -> dict[str, Any]:
    """Under 20 the rate is k of n. At 20 or above, the Wilson range.

    Zero wholly priced runs carry a bound: Wilson's upper end when the window
    has other runs, and a stated bound when it has none.
    """
    low = high = None
    kind = "measured"
    bound = None
    if n >= MIN_JUDGED:
        span = _wilson(k, n)
        if span is not None:
            low, high = span
            kind = "estimated"
    elif k == 0 and n > 0:
        span = _wilson(0, n)
        if span is not None:
            low, high = span
    elif k == 0:
        bound = _BOUND
    return {"k": k, "n": n, "kind": kind, "low": low, "high": high, "bound": bound}


def _span(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "window_start": _iso(start) or "",
        "window_end": _iso(end) or "",
        "window_length": _length(start, end),
    }


def _length(start: datetime, end: datetime) -> str:
    minutes = max(0, int(round((end - start).total_seconds() / 60)))
    hours, mins = divmod(minutes, 60)
    if hours and mins == 0:
        return "1 hour" if hours == 1 else f"{hours} hours"
    if hours:
        return f"{hours} h {mins} min"
    if minutes == 0:
        return "under 1 min"
    return "1 min" if minutes == 1 else f"{minutes} min"


def _day(moment: datetime) -> str:
    shown = moment
    if shown.tzinfo is not None:
        shown = shown.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{shown.day} {_MONTHS[shown.month - 1]} {shown.year}"


def _money(value: float) -> float:
    return float(f"{value:.4f}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
