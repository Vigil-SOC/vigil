"""How sure the investigations were, and whether the closures agreed.

Both are figures for the Production Start Report. Neither is a check, and
neither is called from a Day 1 run. A failed read is not-checked for that
module only. The record stores counts, not a case id or an account name.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from core.detections.inventory.accurate import MIN_JUDGED
from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
from core.reporting.analytics_service import (
    _CLOSURE_VERDICTS,
    _CONFIDENCES,
    _wilson,
    agent_agreement,
)
from core.reporting.day1 import DAY1_TRIGGERS, _iso, _parse
from core.storage.models import Case, CaseClosureInfo, Finding, ReportRecord
from core.storage.models.base import case_findings
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

_LEVELS = (*_CONFIDENCES, "unstated")
_SKIPPED = (SAMPLE_DATA_SOURCE, "probe")
# Closure categories, in the words the history section already uses.
_CLOSURE_LABEL = {
    "resolved": "true positive",
    "false_positive": "false positive",
    "unable_to_resolve": "undetermined",
    "duplicate": "duplicate",
    "unspecified": "unspecified",
}
_SINCE_LABELS = (
    "true positive",
    "false positive",
    "undetermined",
    "duplicate",
    "unspecified",
)
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
_CENSUS = text("""
    SELECT
        CASE
            WHEN payload->>'confidence' IN ('low', 'medium', 'high')
            THEN payload->>'confidence'
            ELSE 'unstated'
        END AS level,
        count(*)::int AS terminals,
        count(*) FILTER (
            WHERE lower(btrim(coalesce(payload->>'disposition', '')))
                 IN ('inconclusive', 'needs_review')
        )::int AS abstained
    FROM agent_events
    WHERE kind = 'terminal'
      AND run_kind = 'investigate'
      AND ts >= (:start AT TIME ZONE 'UTC')
      AND ts <= (:end AT TIME ZONE 'UTC')
    GROUP BY 1
""")
_NO_WINDOW = "not checked: the production window has no start."
_CENSUS_FAILED = "not checked: could not read investigation terminals."
_CLOSURE_FAILED = "not checked: could not read closures."
_NO_DAY1 = "not checked: no Day 1 Report"
_ESTIMATE = "the model's own estimate, not checked against outcomes"
_TRIAGE = "Triage confidence is left out."
_TRIAGE_VIEW = "A person's view of a triage verdict is not recorded."
_NOT_MATCHED = "different alerts, not matched one to one"
_SAME_SIDE = (
    "share of scored closures where the investigation and the closure"
    " took the same side"
)
_OPPOSITE = (
    "share of scored closures where the investigation and the closure"
    " took opposite sides"
)
_ABSTAINED = (
    "share of a person's closures where the investigation said"
    " inconclusive or needs review"
)
_ALONE = "share of closures Vigil closed alone"
_UNCHECKED = "share of agreements where the evidence was not opened"
_FALSE = "sampled false closes among draws a person answered"
_SINCE = "share of cases closed in the window with a finding from this source"
_LEVEL_SHARE = "share of scored closures at this confidence"


def confidence(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """Investigation terminals in the window, by the confidence word stored."""
    bounds = _bounds(started_at, window_end)
    if bounds is None:
        return {"not_checked": _NO_WINDOW}
    try:
        with unit_of_work() as session:
            return _confidence(session, *bounds)
    except Exception as exc:  # noqa: BLE001 - this module only; the report continues
        logger.error("production confidence could not be read: %s", type(exc).__name__)
        return {"not_checked": _CENSUS_FAILED}


def agreement(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """Closure comparison for the window, and the previous process beside it."""
    bounds = _bounds(started_at, window_end)
    if bounds is None:
        return {"not_checked": _NO_WINDOW}
    try:
        with unit_of_work() as session:
            return _agreement(session, *bounds)
    except Exception as exc:  # noqa: BLE001 - this module only; the report continues
        logger.error("production agreement could not be read: %s", type(exc).__name__)
        return {"not_checked": _CLOSURE_FAILED}


def _bounds(
    started_at: Optional[str], window_end: datetime
) -> Optional[tuple[datetime, datetime]]:
    start = _parse(started_at)
    end = _parse(window_end)
    if start is None or end is None:
        return None
    return start, end


def _confidence(session: Session, start: datetime, end: datetime) -> dict[str, Any]:
    counted = {level: {"count": 0, "abstained": 0} for level in _LEVELS}
    for row in session.execute(_CENSUS, {"start": start, "end": end}):
        level = row.level if row.level in _LEVELS else "unstated"
        counted[level] = {"count": int(row.terminals), "abstained": int(row.abstained)}
    return {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "label": _ESTIMATE,
        "triage": _TRIAGE,
        "abstained": sum(row["abstained"] for row in counted.values()),
        "levels": [{"level": level, **row} for level, row in counted.items()],
    }


def _agreement(session: Session, start: datetime, end: datetime) -> dict[str, Any]:
    rows = list(session.execute(_CLOSURE_VERDICTS, {"start": start, "end": end}))
    dropped = _restricted(session, {row.case_id for row in rows})
    # Every reader: a case that names readers is left out. Not case_read_clause.
    overall = agent_agreement(session, start, end, readable=_open_case())["overall"]
    return {
        "window_start": _iso(start),
        "window_end": _iso(end),
        "note": _TRIAGE_VIEW,
        "restricted_left_out": dropped,
        "figures": _figures(overall, start, end),
        "by_confidence": _by_confidence(overall, start, end),
        "previous": _previous(session, start, end),
    }


def _figures(overall: dict[str, Any], start: datetime, end: datetime) -> list[dict]:
    agreed = int(overall["agreed"])
    disagreed = int(overall["disagreed"])
    abstained = int(overall["abstained"])
    unchecked = int(overall["agreed_unchecked"])
    alone = int(overall["closed_by_agent_alone"])
    undetermined = int(overall["closed_without_determination"])
    scored = agreed + disagreed
    person = scored + abstained + unchecked + undetermined
    return [
        _figure("Agreed", agreed, scored, _SAME_SIDE, start, end),
        _figure("Disagreed", disagreed, scored, _OPPOSITE, start, end),
        _figure("Abstained", abstained, person, _ABSTAINED, start, end),
        _figure("Closed by Vigil alone", alone, person + alone, _ALONE, start, end),
        _figure(
            "Agreed without the evidence opened",
            unchecked,
            agreed + unchecked,
            _UNCHECKED,
            start,
            end,
        ),
        _figure(
            "Sampled false closes",
            int(overall["false_closes"]),
            int(overall["answered"]),
            _FALSE,
            start,
            end,
        ),
    ]


def _by_confidence(
    overall: dict[str, Any], start: datetime, end: datetime
) -> dict[str, list[dict]]:
    """Agreement beside a confidence word. Only a level the tally already has."""
    stored = overall.get("by_confidence") or {}
    out: dict[str, list[dict]] = {}
    for level in _LEVELS:
        row = stored.get(level) or {}
        agreed = int(row.get("agreed") or 0)
        disagreed = int(row.get("disagreed") or 0)
        scored = agreed + disagreed
        if scored == 0:
            continue
        out[level] = [
            _figure("Agreed", agreed, scored, _LEVEL_SHARE, start, end),
            _figure("Disagreed", disagreed, scored, _LEVEL_SHARE, start, end),
        ]
    return out


def _previous(session: Session, start: datetime, end: datetime) -> dict[str, Any]:
    record = (
        session.query(ReportRecord)
        .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
        .order_by(ReportRecord.recorded_at.desc())
        .first()
    )
    if record is None:
        return {"not_checked": _NO_DAY1}
    evidence = record.evidence if isinstance(record.evidence, dict) else {}
    before = evidence.get("history.before")
    sources = before.get("sources") if isinstance(before, dict) else None
    if not isinstance(sources, list) or not sources:
        return {
            "not_checked": (
                "not checked: the Day 1 Report of "
                f"{_day(record.recorded_at)} has no history sections"
            )
        }
    return {
        "label": _NOT_MATCHED,
        "run_id": record.run_id,
        "recorded_at": _iso(record.recorded_at),
        "before": _before_sources(sources),
        "since": _since(session, start, end),
        "skipped": _skipped(session, start, end),
        "restricted_left_out": _restricted_closed(session, start, end),
    }


def _before_sources(sources: list) -> list[dict[str, Any]]:
    copied = []
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("source"), str):
            continue
        figures = []
        for figure in source.get("dispositions") or []:
            kept = _copied_figure(figure)
            if kept is not None:
                figures.append(kept)
        copied.append({"source": source["source"], "dispositions": figures})
    return copied


def _copied_figure(figure: Any) -> Optional[dict[str, Any]]:
    if not isinstance(figure, dict):
        return None
    label = figure.get("label")
    if not isinstance(label, str) or not isinstance(figure.get("k"), int):
        return None
    if not isinstance(figure.get("n"), int):
        return None
    kind = figure.get("kind")
    if kind not in ("measured", "estimated"):
        return None
    return {
        "label": label,
        "k": figure["k"],
        "n": figure["n"],
        "kind": kind,
        "definition": (
            figure.get("definition")
            if isinstance(figure.get("definition"), str)
            else ""
        ),
        "window_start": (
            figure.get("window_start")
            if isinstance(figure.get("window_start"), str)
            else ""
        ),
        "window_end": (
            figure.get("window_end")
            if isinstance(figure.get("window_end"), str)
            else ""
        ),
        "low": (
            figure.get("low") if isinstance(figure.get("low"), (int, float)) else None
        ),
        "high": (
            figure.get("high") if isinstance(figure.get("high"), (int, float)) else None
        ),
    }


def _since(session: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            Finding.data_source,
            CaseClosureInfo.closure_category,
            func.count(func.distinct(Case.case_id)),
        )
        .select_from(Case)
        .join(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .join(case_findings, case_findings.c.case_id == Case.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(*_closed(start, end), Finding.data_source.notin_(_SKIPPED))
        .group_by(Finding.data_source, CaseClosureInfo.closure_category)
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for data_source, category, count in rows:
        if not isinstance(data_source, str) or not data_source:
            continue
        label = _CLOSURE_LABEL.get(
            category, category if isinstance(category, str) else "unspecified"
        )
        bucket = counts.setdefault(data_source, {})
        bucket[label] = bucket.get(label, 0) + int(count)
    sources = []
    for name in sorted(counts):
        bucket = counts[name]
        n = sum(bucket.values())
        figures = [
            _figure(label, bucket.get(label, 0), n, _SINCE, start, end)
            for label in _SINCE_LABELS
        ]
        extra = sorted(label for label in bucket if label not in _SINCE_LABELS)
        figures.extend(
            _figure(label, bucket[label], n, _SINCE, start, end) for label in extra
        )
        sources.append({"source": name, "dispositions": figures})
    return sources


def _skipped(session: Session, start: datetime, end: datetime) -> dict[str, int]:
    rows = session.execute(
        select(Finding.data_source, func.count(func.distinct(Finding.finding_id)))
        .select_from(Case)
        .join(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .join(case_findings, case_findings.c.case_id == Case.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(*_closed(start, end), Finding.data_source.in_(_SKIPPED))
        .group_by(Finding.data_source)
    ).all()
    counted = {name: int(count) for name, count in rows}
    return {name: counted.get(name, 0) for name in _SKIPPED}


def _restricted_closed(session: Session, start: datetime, end: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(Case.case_id)))
            .select_from(Case)
            .join(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
            .where(
                Case.status == "closed",
                CaseClosureInfo.closed_at >= start,
                CaseClosureInfo.closed_at <= end,
                _restricted_case(),
            )
        )
        or 0
    )


def _restricted(session: Session, case_ids: set[str]) -> int:
    if not case_ids:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(Case)
            .where(Case.case_id.in_(case_ids), _restricted_case())
        )
        or 0
    )


def _closed(start: datetime, end: datetime) -> tuple:
    return (
        Case.status == "closed",
        CaseClosureInfo.closed_at >= start,
        CaseClosureInfo.closed_at <= end,
        _open_case(),
    )


def _open_case():
    return and_(
        func.coalesce(func.cardinality(Case.reader_principal_ids), 0) == 0,
        func.coalesce(func.cardinality(Case.reader_team_ids), 0) == 0,
    )


def _restricted_case():
    return or_(
        func.coalesce(func.cardinality(Case.reader_principal_ids), 0) > 0,
        func.coalesce(func.cardinality(Case.reader_team_ids), 0) > 0,
    )


def _figure(
    label: str, k: int, n: int, definition: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Under 20 the rate is the count. At 20 or above, the Wilson range, including zero."""
    low = high = None
    kind = "measured"
    if n >= MIN_JUDGED:
        span = _wilson(k, n)
        if span is not None:
            low, high = span
            kind = "estimated"
    return {
        "label": label,
        "k": k,
        "n": n,
        "kind": kind,
        "definition": definition,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "low": low,
        "high": high,
    }


def _day(moment: datetime) -> str:
    shown = moment
    if shown.tzinfo is not None:
        shown = shown.astimezone(timezone.utc).replace(tzinfo=None)
    return f"{shown.day} {_MONTHS[shown.month - 1]} {shown.year}"
