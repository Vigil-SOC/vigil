"""/api/logs: keep telemetry events and search them, every source in one schema.

A connector or forwarder posts batches to POST /events; the Logs page reads
/search, /sources and /events/{id}. Posting needs findings.write,
the permission that already covers importing findings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert

from core.auth.permissions import permission_gate
from core.log_events.query import parse, to_record, to_row, where
from core.routing import Auth, RouterMeta
from core.storage.models.log_event import LogEvent
from core.storage.unit_of_work import unit_of_work

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/logs", tags=["logs"], auth=Auth.REQUIRED)

MAX_LIMIT = 500
MAX_BATCH = 1000

_WRITE = permission_gate("findings.write")


def _when(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Not a time: {value}")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@router.get("/search")
def search_logs(
    q: str = Query(
        "",
        max_length=1000,
        description='Search: free words, "phrases", key:value, -exclude, loglm>0.7',
    ),
    start: Optional[str] = Query(
        None, description="ISO time; default 24 hours before end"
    ),
    end: Optional[str] = Query(None, description="ISO time; default now"),
    sources: Optional[str] = Query(
        None, description="Comma-separated source keys to keep"
    ),
    limit: int = Query(150, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    buckets: int = Query(48, ge=6, le=240),
) -> Dict[str, Any]:
    """Newest first. ``by_source`` counts every source the search matched, before
    the ``sources`` filter, so the source chips can show what turning one on adds.
    ``histogram`` spreads the matched events over the window."""
    t1 = _when(end) or datetime.now(timezone.utc)
    t0 = _when(start) or t1 - timedelta(hours=24)
    if t0 >= t1:
        raise HTTPException(status_code=422, detail="start must be before end")
    in_window = and_(LogEvent.time >= t0, LogEvent.time <= t1)
    matched = where(q)
    base = and_(in_window, matched) if matched is not None else in_window
    keep = [s for s in (sources or "").split(",") if s]
    shown = and_(base, LogEvent.source.in_(keep)) if keep else base

    span = (t1 - t0).total_seconds() / buckets
    with unit_of_work() as session:
        by_source = dict(
            session.execute(
                select(LogEvent.source, func.count())
                .where(base)
                .group_by(LogEvent.source)
            ).all()
        )
        total = session.execute(
            select(func.count()).select_from(LogEvent).where(shown)
        ).scalar_one()
        rows = (
            session.execute(
                select(LogEvent)
                .where(shown)
                .order_by(LogEvent.time.desc(), LogEvent.event_id.desc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        index = func.least(
            buckets - 1,
            func.floor((func.extract("epoch", LogEvent.time) - t0.timestamp()) / span),
        ).label("b")
        hist = session.execute(
            select(
                index,
                func.count(),
                func.sum(case((LogEvent.incident.isnot(None), 1), else_=0)),
            )
            .where(shown)
            .group_by(index)
        ).all()
        newest = session.execute(select(func.max(LogEvent.time))).scalar_one_or_none()
        events = [to_row(e) for e in rows]

    counts = [0] * buckets
    incident = [0] * buckets
    for b, n, inc in hist:
        if b is not None and 0 <= int(b) < buckets:
            counts[int(b)] = int(n)
            incident[int(b)] = int(inc or 0)
    return {
        "q": q,
        "terms": [
            {"key": t.key, "value": t.value, "negate": t.negate} for t in parse(q)
        ],
        "start": t0.isoformat().replace("+00:00", "Z"),
        "end": t1.isoformat().replace("+00:00", "Z"),
        "total": total,
        "events": events,
        "by_source": by_source,
        "histogram": {"bucket_seconds": span, "counts": counts, "incident": incident},
        "newest": newest.isoformat().replace("+00:00", "Z") if newest else None,
    }


@router.get("/sources")
def log_sources() -> Dict[str, Any]:
    """Each source: how many events Vigil holds, and when the last one arrived."""
    with unit_of_work() as session:
        rows = session.execute(
            select(
                LogEvent.source,
                func.count(),
                func.min(LogEvent.time),
                func.max(LogEvent.time),
            ).group_by(LogEvent.source)
        ).all()
    return {
        "sources": [
            {
                "source": s,
                "events": int(n),
                "first": a.isoformat().replace("+00:00", "Z"),
                "last": b.isoformat().replace("+00:00", "Z"),
            }
            for s, n, a, b in sorted(rows)
        ]
    }


@router.get("/events/{event_id}")
def get_event(event_id: str) -> Dict[str, Any]:
    with unit_of_work() as session:
        e = session.get(LogEvent, event_id)
        if e is None:
            raise HTTPException(status_code=404, detail=f"No event {event_id}")
        return to_row(e)


class IngestEvent(BaseModel):
    """One event as a sender posts it. id is the sender's own stable id, so
    posting the same event twice stores it once."""

    id: str = Field(min_length=1, max_length=64)
    time: datetime
    source: str = Field(min_length=1, max_length=32)
    event_class: str = Field("", max_length=64)
    severity: str = Field("info", max_length=16)
    src_ip: Optional[str] = Field(None, max_length=64)
    src_host: Optional[str] = Field(None, max_length=255)
    dst_ip: Optional[str] = Field(None, max_length=64)
    dst_host: Optional[str] = Field(None, max_length=255)
    dst_port: Optional[int] = Field(None, ge=0, le=65535)
    user: Optional[str] = Field(None, max_length=128)
    action: Optional[str] = Field(None, max_length=64)
    summary: str = ""
    mitre: Optional[str] = Field(None, max_length=32)
    loglm: Optional[float] = Field(None, ge=0, le=1)
    incident: Optional[str] = Field(None, max_length=32)
    tags: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class IngestBatch(BaseModel):
    events: List[IngestEvent] = Field(min_length=1, max_length=MAX_BATCH)


@router.post("/events", dependencies=[_WRITE])
def ingest_events(batch: IngestBatch) -> Dict[str, int]:
    """Store a batch. Events whose id is already held are skipped, not updated."""
    rows = []
    for e in batch.events:
        data = e.model_dump()
        if data["time"].tzinfo is None:
            data["time"] = data["time"].replace(tzinfo=timezone.utc)
        rows.append(to_record(data))
    with unit_of_work() as session:
        stored = session.execute(
            insert(LogEvent)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[LogEvent.event_id])
            .returning(LogEvent.event_id)
        ).all()
    return {
        "received": len(rows),
        "stored": len(stored),
        "skipped": len(rows) - len(stored),
    }
