"""Tuning since Production Start.

Read-only. Alerts stored in the production window are grouped by rule, when
the catalog resolves one, or by source and title. Nothing here writes a
rule's Accurate check or an exclusion.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.detections.inventory import catalog
from core.detections.inventory.accurate import (
    FAIL_BELOW,
    FALSE_CAUSES,
    FALSE_SPLIT,
    MIN_JUDGED,
    _closes,
    _findings,
    determination_of,
)
from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
from core.reporting.analytics_service import _wilson
from core.reporting.day1 import FALLBACK, ON_DEMAND, READINESS, _iso, _parse
from core.reporting.history_sections import _row as _history_ticket
from core.storage.ip_address import normalize_ip
from core.storage.models import Case, Finding, ReportRecord, case_findings
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# core cannot import services.daemon.probes. The probe source is this string.
PROBE = "probe"
_TOO_FEW = "too few to judge yet"
_DAY1 = (READINESS, FALLBACK, ON_DEMAND)
_TOTALS = ("real", "judged", "analyst", "agent")
# The five causes, then who marked it false and a winning false-positive close.
_CAUSE_KEYS = (
    *FALSE_CAUSES,
    *(key for key in FALSE_SPLIT if key not in FALSE_CAUSES),
)


def tuning(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """Candidates in the production window, or why this read was not checked."""
    try:
        start = _parse(started_at)
        end = _naive(window_end)
        if start is None:
            return {"not_checked": "not checked: no Production Start."}
        if end < start:
            return {"not_checked": "not checked: the window ends before it starts."}
        return _tuning(start, end)
    except Exception as exc:  # noqa: BLE001 - the other modules still complete
        logger.error(
            "Production Start Report could not read tuning: %s", type(exc).__name__
        )
        return {
            "not_checked": (
                f"not checked: could not read tuning ({type(exc).__name__})."
            )
        }


def _tuning(start: datetime, end: datetime) -> dict[str, Any]:
    window = {"start": _iso(start), "end": _iso(end)}
    with unit_of_work() as session:
        findings = _findings(
            session,
            Finding.created_at >= start,
            Finding.created_at <= end,
            sources=None,
        )
        titles = _titles(session, start, end)
        restricted = _restricted(session, start, end)
        kept: dict[str, dict[str, Any]] = {}
        left = {"sample": 0, "probe": 0, "restricted": 0}
        for fid, row in findings.items():
            source = row.get("data_source") or ""
            if source == SAMPLE_DATA_SOURCE:
                left["sample"] += 1
            elif source == PROBE:
                left["probe"] += 1
            elif fid in restricted:
                left["restricted"] += 1
            else:
                kept[fid] = row
        resolved = catalog.resolve_findings(session, kept, full=False) if kept else {}
        closes = _closes(
            session,
            Finding.created_at >= start,
            Finding.created_at <= end,
            sources=None,
        )
        groups = _groups(kept, titles, resolved, closes)
        history = _history(session)
    reached = [group for group in groups.values() if group["judged"] >= MIN_JUDGED]
    body: dict[str, Any] = {"left_out": left, "window": window}
    if not reached:
        body["line"] = _TOO_FEW
        body["groups"] = [
            _busy(group, window) for group in sorted(groups.values(), key=_volume)
        ]
        return body
    failing = [
        group for group in reached if group["real"] < FAIL_BELOW * group["judged"]
    ]
    failing.sort(key=_worst)
    body["candidates"] = [_candidate(group, window, history) for group in failing]
    return body


def _titles(
    session: Session, start: datetime, end: datetime
) -> dict[str, Optional[str]]:
    """The stored title. NULL and \"\" stay apart: they are different groups."""
    rows = session.execute(
        select(Finding.finding_id, Finding.title).where(
            Finding.created_at >= start,
            Finding.created_at <= end,
        )
    ).all()
    return {fid: title if isinstance(title, str) else None for fid, title in rows}


def _restricted(session: Session, start: datetime, end: datetime) -> set[str]:
    """Findings on a case that names a reader. Counted once, for every reader."""
    named = or_(
        func.coalesce(func.cardinality(Case.reader_principal_ids), 0) > 0,
        func.coalesce(func.cardinality(Case.reader_team_ids), 0) > 0,
    )
    rows = session.execute(
        select(case_findings.c.finding_id)
        .join(Case, Case.case_id == case_findings.c.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(Finding.created_at >= start, Finding.created_at <= end, named)
    ).all()
    return {fid for (fid,) in rows}


def _groups(
    kept: dict[str, dict[str, Any]],
    titles: dict[str, Optional[str]],
    resolved: dict[str, dict[str, Any]],
    closes: dict[str, list],
) -> dict[tuple, dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for fid, row in kept.items():
        mapped = resolved.get(fid) or {}
        title = titles.get(fid)
        source = row.get("data_source") or ""
        if (
            mapped.get("resolved")
            and mapped.get("source") in catalog.RESOLVES
            and mapped.get("rule_id") is not None
        ):
            key = ("rule", mapped["rule_id"])
            group = groups.get(key)
            if group is None:
                group = _blank(
                    "rule",
                    str(mapped.get("source") or ""),
                    "",
                    mapped.get("name") if isinstance(mapped.get("name"), str) else None,
                    (
                        mapped.get("native_key")
                        if isinstance(mapped.get("native_key"), str)
                        else None
                    ),
                    mapped.get("rule_id"),
                )
                groups[key] = group
        else:
            key = ("title", source, title)
            group = groups.get(key)
            if group is None:
                group = _blank("title", source, title, None, None, None)
                groups[key] = group
        group["alerts"] += 1
        won = determination_of(closes.get(fid, []), *row["noise"])
        if won is None:
            continue
        group["judged"] += 1
        group["real"] += int(won.real)
        group["analyst" if won.analyst else "agent"] += 1
        if won.real:
            continue
        group[won.cause or "not_given"] += 1
        group["false_person" if won.analyst else "false_agent"] += 1
        if won.case_close:
            group["case_close"] += 1
    return groups


def _blank(
    kind: str,
    source: str,
    title: Optional[str],
    name: Optional[str],
    native_key: Optional[str],
    rule_id: Any,
) -> dict[str, Any]:
    group: dict[str, Any] = {
        "kind": kind,
        "source": source,
        "title": title,
        "name": name,
        "native_key": native_key,
        "rule_id": rule_id,
        "alerts": 0,
    }
    for key in (*_TOTALS, *_CAUSE_KEYS):
        group[key] = 0
    return group


def _history(
    session: Session,
) -> Optional[tuple[str, set[str], set[Optional[str]]]]:
    """Rule ids and titles on the latest Day 1 tuning list, when that key exists."""
    record = (
        session.query(ReportRecord)
        .filter(ReportRecord.trigger.in_(_DAY1))
        .order_by(ReportRecord.recorded_at.desc())
        .first()
    )
    if record is None:
        return None
    evidence = record.evidence or {}
    if "history.tuning" not in evidence:
        return None
    stored = evidence.get("history.tuning")
    candidates = stored.get("candidates") if isinstance(stored, dict) else None
    rules: set[str] = set()
    titles: set[Optional[str]] = set()
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("group")
        if kind == "entity":
            continue
        if kind == "rule":
            rule_id = row.get("rule_id")
            if isinstance(rule_id, str) and rule_id:
                rules.add(rule_id)
            continue
        if kind != "title":
            continue
        ticket, note = _history_ticket(
            session, row.get("ref") or {}, record.recorded_at
        )
        if note or ticket is None:
            continue
        # Same read as a history title candidate: stripped, and blank is None.
        raw = getattr(ticket, "title", None)
        label = (raw.strip() if isinstance(raw, str) else "") or None
        titles.add(label)
    return _iso(record.recorded_at) or "", rules, titles


def _on_list(
    group: dict[str, Any], rules: set[str], titles: set[Optional[str]]
) -> bool:
    if group["kind"] == "rule":
        return any(
            isinstance(key, str) and key and key in rules
            for key in (group.get("native_key"), group.get("name"))
        )
    return group["title"] in titles


def _base(group: dict[str, Any], window: dict[str, Optional[str]]) -> dict[str, Any]:
    title = group["title"] if group["kind"] == "title" else None
    return {
        "group": group["kind"],
        "source": group["source"],
        "title": title,
        "name": group["name"],
        "native_key": group["native_key"],
        "rule_id": group["rule_id"],
        "address": _public_address(title) if group["kind"] == "title" else None,
        "alerts": group["alerts"],
        "real": group["real"],
        "judged": group["judged"],
        "analyst": group["analyst"],
        "agent": group["agent"],
        "window_start": window["start"],
        "window_end": window["end"],
    }


def _busy(group: dict[str, Any], window: dict[str, Optional[str]]) -> dict[str, Any]:
    benign = group["judged"] - group["real"]
    return {
        **_base(group, window),
        "benign_k": benign,
        "benign_n": group["judged"],
    }


def _candidate(
    group: dict[str, Any],
    window: dict[str, Optional[str]],
    history: Optional[tuple[str, set[str], set[Optional[str]]]],
) -> dict[str, Any]:
    judged = group["judged"]
    real = group["real"]
    span = _wilson(real, judged)
    low, high = span if span is not None else (None, None)
    row = {
        **_base(group, window),
        "benign": judged - real,
        "true_positive_share": round(real / judged, 4) if judged else None,
        "low": low,
        "high": high,
        "causes": {key: group[key] for key in _CAUSE_KEYS},
    }
    if history is not None:
        recorded_at, rules, titles = history
        row["on_history_tuning"] = _on_list(group, rules, titles)
        row["history_recorded_at"] = recorded_at
    return row


def _public_address(title: Optional[str]) -> Optional[str]:
    addr = normalize_ip(title)
    if addr is None:
        return None
    try:
        if not ipaddress.ip_address(addr).is_global:
            return None
    except ValueError:
        return None
    return addr


def _volume(group: dict[str, Any]) -> tuple:
    return (
        -group["alerts"],
        group["source"],
        _label(group),
        group["title"] is None,
        group["kind"],
    )


def _worst(group: dict[str, Any]) -> tuple:
    judged = group["judged"] or 1
    return (
        group["real"] / judged,
        -group["judged"],
        group["source"],
        _label(group),
        group["title"] is None,
    )


def _label(group: dict[str, Any]) -> str:
    if group["kind"] == "rule":
        return group["name"] or group["native_key"] or ""
    return group["title"] or ""


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
