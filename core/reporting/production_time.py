"""Time to first verdict and time to close, for the Production Start Report.

Per source, inside the production window. No checks. The Day 1 "before" line
is copied off the latest Day 1 record; import tables are not read.
"""

from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.cases.case_status import CLOSED
from core.cases.close_time import closed_at
from core.detections.inventory.accurate import MIN_JUDGED
from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
from core.reporting.day1 import DAY1_TRIGGERS, _iso, _parse
from core.reporting.history_sections import _duration, _percentile
from core.storage.models import (
    Case,
    CaseClosureInfo,
    Finding,
    ReportRecord,
    case_findings,
)
from core.storage.unit_of_work import unit_of_work

# Sources that store the fetch time when the source gave none. The adapters
# stay as they are; the report says the alerts cannot be told apart.
FETCH_TIME = frozenset(
    {
        "microsoft_defender",
        "splunk",
        "crowdstrike",
        "elastic",
        "aws_security_hub",
        "opensearch",
        "azure_sentinel",
        "cloudflare_cloudy",
    }
)
FETCH_TIME_LINE = "These alerts cannot be told apart from the fetch time."
CLOSE_NOTE = "A short window over-represents quick closes."
NO_DAY1 = "not checked: no Day 1 Report"
_BANDS = ("under an hour", "under a day", "longer")
_HOUR = 3600
_DAY = 86400
_PROBE = "probe"


def report(window: dict) -> dict[str, Any]:
    """The body stored at ``evidence["production.time"]``."""
    start = _parse(window.get("start"))
    end = _parse(window.get("end"))
    with unit_of_work() as session:
        sources = _sources(session, start, end) if _span(start, end) else []
        before = _before(session)
    return {
        "window": {
            "start": _iso(start),
            "end": _iso(end),
            "length": _length(start, end),
        },
        "close_note": CLOSE_NOTE,
        "sources": sources,
        "before": before,
    }


def _span(start: Optional[datetime], end: Optional[datetime]) -> bool:
    return start is not None and end is not None and end >= start


def _length(start: Optional[datetime], end: Optional[datetime]) -> Optional[str]:
    if not _span(start, end):
        return None
    return _duration((end - start).total_seconds())


def _sources(session: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    verdicts = _verdicts(session, start, end)
    closes = _closes(session, start, end)
    ids = [row.finding_id for row in verdicts] + [row.finding_id for row in closes]
    restricted = _restricted(session, ids)
    by_source: dict[str, dict[str, Any]] = {}
    for row in verdicts:
        _take(
            _bucket(by_source, row.data_source),
            "verdict",
            _reason(row.data_source, row.finding_id in restricted),
            (
                None
                if row.timestamp is None or row.first_verdict_at is None
                else _seconds(row.timestamp, row.first_verdict_at)
            ),
        )
    for row in closes:
        _take(
            _bucket(by_source, row.data_source),
            "close",
            _reason(row.data_source, row.finding_id in restricted),
            (
                None
                if row.timestamp is None or row.closed_at is None
                else _seconds(row.timestamp, row.closed_at)
            ),
        )
    sources = []
    for name in sorted(by_source):
        held = by_source[name]
        verdict = _figure(held["verdict"])
        close = _figure(held["close"])
        if _quiet(verdict) and _quiet(close):
            continue
        estimated = name in FETCH_TIME
        sources.append(
            {
                "source": name,
                "kind": "estimated" if estimated else "measured",
                "fetch_time": FETCH_TIME_LINE if estimated else None,
                "time_to_first_verdict": verdict,
                "time_to_close": close,
            }
        )
    return sources


def _verdicts(session: Session, start: datetime, end: datetime):
    verdict = Finding.ai_enrichment["ai_triage"]["result"]["verdict"].astext
    return session.execute(
        select(
            Finding.finding_id,
            Finding.data_source,
            Finding.timestamp,
            Finding.first_verdict_at,
        ).where(
            Finding.timestamp >= start,
            Finding.timestamp <= end,
            verdict.isnot(None),
            verdict != "",
        )
    ).all()


def _closes(session: Session, start: datetime, end: datetime):
    closed = closed_at()
    return session.execute(
        select(
            Finding.finding_id,
            Finding.data_source,
            Finding.timestamp,
            func.min(closed).label("closed_at"),
        )
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .join(Case, Case.case_id == case_findings.c.case_id)
        .outerjoin(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .where(Case.status == CLOSED, closed >= start, closed <= end)
        .group_by(Finding.finding_id, Finding.data_source, Finding.timestamp)
    ).all()


def _restricted(session: Session, finding_ids: list[str]) -> set[str]:
    if not finding_ids:
        return set()
    rows = session.scalars(
        select(case_findings.c.finding_id)
        .join(Case, Case.case_id == case_findings.c.case_id)
        .where(
            case_findings.c.finding_id.in_(finding_ids),
            or_(
                func.coalesce(func.cardinality(Case.reader_principal_ids), 0) > 0,
                func.coalesce(func.cardinality(Case.reader_team_ids), 0) > 0,
            ),
        )
        .distinct()
    )
    return set(rows)


def _reason(data_source: str, restricted: bool) -> Optional[str]:
    """One reason, so a probe on a restricted case is counted once."""
    if data_source == _PROBE:
        return "probe"
    if data_source == SAMPLE_DATA_SOURCE:
        return "sample"
    if restricted:
        return "restricted"
    return None


def _bucket(by_source: dict[str, dict[str, Any]], source: str) -> dict[str, Any]:
    held = by_source.get(source)
    if held is None:
        held = {"verdict": _empty(), "close": _empty()}
        by_source[source] = held
    return held


def _empty() -> dict[str, Any]:
    return {
        "measured": [],
        "not_recorded": 0,
        "left_out": {"restricted": 0, "sample": 0, "probe": 0},
    }


def _take(
    held: dict[str, Any], which: str, reason: Optional[str], seconds: Optional[float]
) -> None:
    bucket = held[which]
    if reason is not None:
        bucket["left_out"][reason] += 1
        return
    if seconds is None:
        bucket["not_recorded"] += 1
        return
    bucket["measured"].append(seconds)


def _figure(bucket: dict[str, Any]) -> dict[str, Any]:
    measured: list[float] = bucket["measured"]
    count = len(measured)
    median = p90 = bands = None
    if count >= MIN_JUDGED:
        median = _duration(_percentile(measured, 0.5))
        p90 = _duration(_percentile(measured, 0.9))
    elif count:
        counts = Counter(_band(seconds) for seconds in measured)
        bands = [{"label": label, "count": counts[label]} for label in _BANDS]
    return {
        "count": count,
        "not_recorded": bucket["not_recorded"],
        "median": median,
        "p90": p90,
        "bands": bands,
        "left_out": dict(bucket["left_out"]),
    }


def _band(seconds: float) -> str:
    if seconds < _HOUR:
        return _BANDS[0]
    if seconds < _DAY:
        return _BANDS[1]
    return _BANDS[2]


def _quiet(figure: dict[str, Any]) -> bool:
    return (
        figure["count"] == 0
        and figure["not_recorded"] == 0
        and not any(figure["left_out"].values())
    )


def _seconds(start: datetime, end: datetime) -> Optional[float]:
    start = _naive(start)
    end = _naive(end)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _naive(value: datetime) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _before(session: Session) -> dict[str, Any]:
    record = (
        session.query(ReportRecord)
        .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
        .order_by(ReportRecord.recorded_at.desc(), ReportRecord.record_id.desc())
        .first()
    )
    if record is None:
        return {"recorded_at": None, "not_checked": NO_DAY1, "sources": []}
    recorded = _iso(record.recorded_at)
    if "history.before" not in (record.modules or []):
        return {
            "recorded_at": recorded,
            "not_checked": (
                "not checked: the Day 1 Report of "
                f"{_day(record.recorded_at)} has no history sections"
            ),
            "sources": [],
        }
    stored = (record.evidence or {}).get("history.before")
    if not isinstance(stored, dict):
        return {
            "recorded_at": recorded,
            "not_checked": (
                "not checked: the Day 1 Report of "
                f"{_day(record.recorded_at)} has no history sections"
            ),
            "sources": [],
        }
    noted = stored.get("not_checked")
    if isinstance(noted, str) and noted:
        return {"recorded_at": recorded, "not_checked": noted, "sources": []}
    sources = []
    for item in stored.get("sources") or []:
        if not isinstance(item, dict) or not isinstance(item.get("source"), str):
            continue
        times = item.get("time_to_close")
        sources.append(
            {
                "source": item["source"],
                "time_to_close": (
                    [copy.deepcopy(row) for row in times if isinstance(row, dict)]
                    if isinstance(times, list)
                    else []
                ),
            }
        )
    return {"recorded_at": recorded, "not_checked": None, "sources": sources}


def _day(moment: datetime) -> str:
    shown = _naive(moment) or moment
    return f"{shown.day} {shown:%b} {shown.year}"
