"""What happened to every alert, and which sources have gone quiet.

Counted at run time and frozen on the production record. Both read SQL.
Neither reads the demo data service, ``filtered_total``, or ``duplicate_window``,
and neither sweeps a SIEM. A failed read is "not checked"; the other modules
still complete.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.detections.inventory import metrics as rule_metrics
from core.detections.inventory.accurate import MIN_JUDGED
from core.detections.inventory.catalog import SIEM_NAMES
from core.federation.store import read_sources
from core.findings.alert_outcomes import finding_states, ttl_expired_alerts
from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
from core.integrations.health import UNHEALTHY
from core.integrations.health import collect as collect_health
from core.integrations.health import health_id
from core.reporting.analytics_service import _wilson
from core.reporting.day1 import (
    NEEDS_A_PERSON,
    NOT_CHECKED,
    PASS,
    _check,
    _iso,
    _parse,
)
from core.response.ceiling import close_held
from core.storage.models import (
    Case,
    DetectionRule,
    DetectionRuleVersion,
    Finding,
)
from core.storage.source_health import LEGACY_POLLED, read_all
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

PROBE = "probe"
QUIET_HOURS = 24
# 95% upper bound on a rate when nothing was seen and there is no denominator.
_ZERO_BOUND = "at most 3 in this window"
DUPLICATES = "not recorded: Vigil keeps no record of duplicates"
FLOOR_LINE = "not recorded"
NOT_STORED = "A duplicate or an alert under the floor is not stored."
_CHUNK = 5000
_TRIAGE = frozenset({"waiting", "scoring"})
# Receivers whose stored alerts carry the source's own name. The daemon
# webhook and Kafka do not: their alerts carry the sender's name, so those
# checks read last_data_at.
_OWN_NAME = {
    "webhook:darktrace": "darktrace",
    "webhook:cloudflare": "cloudflare_cloudy",
}
_HEALTH_OF = {name: source_id for source_id, name in _OWN_NAME.items()}
_ALERT_KINDS = frozenset({"pulled", "kafka", "receiver"})
_SIEM = ("elastic", "splunk", "wazuh")

# Overview's order, with on_case split by _state. One group each.
_GROUPS = (
    ("dismissed", "Dismissed", "Marked as noise."),
    ("needs_you", "Needs you", "On a case that needs a person."),
    ("resolved_person", "Resolved by a person", "On a case a person closed."),
    ("resolved_auto", "Resolved automatically", "On a case closed without a person."),
    ("case_idle", "Idle", "On a case whose runs have ended."),
    ("working", "Working", "On a case still being worked."),
    ("excluded", "Excluded", "Names an excluded address, so triage is skipped."),
    ("scoring", "Scoring", "Queued for triage."),
    ("waiting", "Waiting", "Not picked up."),
)
_LEFT_OUT = (
    (
        "restricted",
        "Restricted cases",
        "On a case that names a reader, left out of every group.",
    ),
    ("sample", "Sample alerts", "Vigil's sample alerts, left out of every group."),
    ("probe", "Probe alerts", "Vigil's known-answer probes, left out of every group."),
)
_GROUP_IDS = frozenset(state for state, _label, _definition in _GROUPS)


def alerts(started_at: Optional[str], window_end: datetime) -> dict[str, Any]:
    """Per source, every finding stored in the window, in one group."""
    try:
        start = _parse(started_at)
        end = _naive(window_end)
        if start is None:
            return {"not_checked": "not checked: no Production Start."}
        if end < start:
            return {"not_checked": "not checked: the window ends before it starts."}
        return _alerts(start, end)
    except Exception as exc:  # noqa: BLE001 - the other modules still complete
        logger.error(
            "Production Start Report could not read alerts: %s", type(exc).__name__
        )
        return {
            "not_checked": (
                f"not checked: could not read stored alerts ({type(exc).__name__})."
            )
        }


def quiet(
    started_at: Optional[str], window_end: datetime
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """One check per switched-on alert source, plus stored Silent and first week."""
    try:
        return _quiet(started_at, _naive(window_end))
    except Exception as exc:  # noqa: BLE001 - one not-checked check, not a partial
        logger.error(
            "Production Start Report could not read quiet sources: %s",
            type(exc).__name__,
        )
        reason = (
            "not checked: could not read sources gone quiet " f"({type(exc).__name__})."
        )
        return [_check("production.quiet", NOT_CHECKED, reason)], None


def _alerts(start: datetime, end: datetime) -> dict[str, Any]:
    with unit_of_work() as session:
        rows = (
            session.query(Finding.finding_id, Finding.data_source)
            .filter(Finding.created_at >= start, Finding.created_at <= end)
            .all()
        )
        kept: dict[str, list[str]] = defaultdict(list)
        left: dict[str, Counter] = defaultdict(Counter)
        for finding_id, data_source in rows:
            source = data_source or "unknown"
            if source == SAMPLE_DATA_SOURCE:
                left[source]["sample"] += 1
            elif source == PROBE:
                left[source]["probe"] += 1
            else:
                kept[source].append(finding_id)
    pending = [
        (source, finding_id) for source, ids in kept.items() for finding_id in ids
    ]
    states = finding_states([finding_id for _source, finding_id in pending])
    with unit_of_work() as session:
        restricted = _restricted_cases(
            session,
            [case_id for _state, case_id in states.values() if case_id],
        )
    counted: list[tuple[str, str]] = []
    for source, finding_id in pending:
        _state, case_id = states.get(finding_id, ("waiting", None))
        # The case the group reads. An older restricted link does not hide it.
        # A dismissal still names that case, and a restricted case stays out of
        # every figure.
        if case_id and case_id in restricted:
            left[source]["restricted"] += 1
        else:
            counted.append((source, finding_id))
    triage_ids = [
        finding_id
        for _source, finding_id in counted
        if states.get(finding_id, ("waiting", None))[0] in _TRIAGE
    ]
    expired = ttl_expired_alerts(triage_ids) if triage_ids else set()
    grouped: dict[str, Counter] = defaultdict(Counter)
    failed: Counter = Counter()
    for source, finding_id in counted:
        state = states.get(finding_id, ("waiting", None))[0]
        if state not in _GROUP_IDS:
            state = "waiting"
        grouped[source][state] += 1
        if finding_id in expired and state in _TRIAGE:
            failed[(source, state)] += 1
    floors = _floors()
    records = read_all()
    span = _span(start, end)
    sources = []
    for source in sorted(set(left) | set(grouped)):
        counts = grouped.get(source, Counter())
        total = sum(counts.values())
        groups = []
        for state, label, definition in _GROUPS:
            figure = _share(counts.get(state, 0), total)
            figure.update(
                {
                    "state": state,
                    "label": label,
                    "definition": definition,
                    "failed_triage": (
                        failed[(source, state)] if state in _TRIAGE else None
                    ),
                    **span,
                }
            )
            groups.append(figure)
        record = records.get(_health_key(source))
        dropped = record.get("dropped_total") if record else None
        sources.append(
            {
                "source": source,
                "groups": groups,
                "left_out": [
                    {
                        "key": key,
                        "label": label,
                        "count": left[source][key],
                        "kind": "measured",
                        "definition": definition,
                        **span,
                    }
                    for key, label, definition in _LEFT_OUT
                ],
                "floor": FLOOR_LINE,
                "min_severity": floors.get(source),
                "dropped": dropped if isinstance(dropped, int) else None,
            }
        )
    return {
        "window": {
            "start": span["window_start"],
            "end": span["window_end"],
            "length": span["window_length"],
        },
        "duplicates": DUPLICATES,
        "close_held": close_held(None),
        "sources": sources,
    }


def _quiet(
    started_at: Optional[str], end: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = _parse(started_at)
    sources = [
        row
        for row in collect_health(end)
        if row.get("switched_on") and row.get("kind") in _ALERT_KINDS
    ]
    sources.sort(key=lambda row: row.get("source_id") or "")
    names = {
        row["source_id"]: _alert_name(row["source_id"], row.get("kind"))
        for row in sources
        if row.get("source_id")
    }
    newest = _newest(end, [name for name in names.values() if name])
    # The last 24 hours of the window, and not before Production Start.
    recent_from = end - timedelta(hours=QUIET_HOURS)
    if start is not None and start > recent_from:
        recent_from = start
    checks = [
        _quiet_check(row, names.get(row.get("source_id")), newest, recent_from, end)
        for row in sources
    ]
    evidence: dict[str, Any] = {
        "window": {
            "start": _iso(start) if start else None,
            "end": _iso(end),
            "length": _length(start, end) if start else None,
        },
        "silent": [],
        "first_week": None,
    }
    with unit_of_work() as session:
        evidence["silent"] = _silent(session)
        # Stored rows only. These helpers do not search a SIEM.
        week_start = rule_metrics._siem_start(session)
        changes = rule_metrics._siem_changes(session, week_start)
        evidence["first_week"] = rule_metrics.first_week(
            week_start, changes, end.replace(tzinfo=timezone.utc)
        )
    return checks, evidence


def _quiet_check(
    row: dict,
    data_source: Optional[str],
    newest: dict[str, datetime],
    recent_from: datetime,
    end: datetime,
) -> dict[str, Any]:
    source_id = str(row.get("source_id") or "source")
    record = _record_clause(source_id, data_source)
    if row.get("state") == UNHEALTHY:
        reason = row.get("reason")
        since = _moment(row.get("since"))
        latest = _shown(since) if since else "time not recorded"
        detail = f"{reason}. " if reason else ""
        return _check(
            f"production.quiet.{source_id}",
            NEEDS_A_PERSON,
            f"{source_id}: unhealthy. {detail}Latest {latest}. {record}",
        )
    when = _arrival(row, data_source, newest)
    # recent_from is inside the window. A time after the window end is newer
    # than the run, so that source is not quiet either.
    if when is not None and when >= recent_from:
        return _check(
            f"production.quiet.{source_id}",
            PASS,
            f"{source_id}: new alert in the last 24 hours of the window. {record}",
        )
    if when is None:
        text = f"{source_id}: no alerts since switched on."
    else:
        hours = max(1, int((end - when).total_seconds() // 3600))
        unit = "hour" if hours == 1 else "hours"
        text = (
            f"{source_id}: no new alert for {hours} {unit}; " "confirm that's expected."
        )
    return _check(
        f"production.quiet.{source_id}",
        NEEDS_A_PERSON,
        f"{text} {record} {NOT_STORED}",
    )


def _arrival(
    row: dict, data_source: Optional[str], newest: dict[str, datetime]
) -> Optional[datetime]:
    if data_source is not None:
        return newest.get(data_source)
    record = row.get("record") or {}
    return _moment(record.get("last_data_at"))


def _alert_name(source_id: str, kind: Optional[str]) -> Optional[str]:
    if source_id in _OWN_NAME:
        return _OWN_NAME[source_id]
    if kind == "pulled":
        # Health id elastic-siem stores findings as elastic. Splunk matches.
        return LEGACY_POLLED.get(source_id, source_id)
    return None


def _health_key(data_source: str) -> str:
    """The health row for a finding's data_source."""
    if data_source in _HEALTH_OF:
        return _HEALTH_OF[data_source]
    return health_id(data_source)


def _record_clause(source_id: str, data_source: Optional[str]) -> str:
    if data_source is None:
        return f"Read last_data_at on {source_id}."
    return f"Read the newest stored alert ({data_source})."


def _newest(end: datetime, names: list[str]) -> dict[str, datetime]:
    if not names:
        return {}
    with unit_of_work() as session:
        rows = (
            session.query(Finding.data_source, func.max(Finding.created_at))
            .filter(Finding.data_source.in_(names), Finding.created_at <= end)
            .group_by(Finding.data_source)
            .all()
        )
    return {source: _naive(moment) for source, moment in rows if moment is not None}


def _silent(session: Session) -> list[dict[str, Any]]:
    version = DetectionRuleVersion
    rows = (
        session.query(DetectionRule.source, DetectionRule.name, version.checks)
        .join(
            version,
            (version.rule_id == DetectionRule.id)
            & (version.version == DetectionRule.latest_version),
        )
        .filter(DetectionRule.source.in_(_SIEM))
        .order_by(DetectionRule.source, DetectionRule.name)
        .all()
    )
    stored = []
    for source, name, checks in rows:
        silent = checks.get("silent") if isinstance(checks, dict) else None
        if not isinstance(silent, dict):
            continue
        result = silent.get("result")
        reason = silent.get("reason")
        stored.append(
            {
                "tool": SIEM_NAMES.get(source, source),
                "name": name,
                "result": result if isinstance(result, str) else "unknown",
                "reason": reason if isinstance(reason, str) else None,
            }
        )
    return stored


def _restricted_cases(session: Session, case_ids: list[str]) -> set[str]:
    """Cases that name a reader. Every reader leaves their alerts out."""
    found: set[str] = set()
    named = or_(
        func.coalesce(func.cardinality(Case.reader_principal_ids), 0) > 0,
        func.coalesce(func.cardinality(Case.reader_team_ids), 0) > 0,
    )
    ids = [case_id for case_id in case_ids if case_id]
    for at in range(0, len(ids), _CHUNK):
        chunk = ids[at : at + _CHUNK]
        rows = session.query(Case.case_id).filter(Case.case_id.in_(chunk), named)
        found.update(case_id for (case_id,) in rows)
    return found


def _floors() -> dict[str, Optional[str]]:
    """Current severity floor, keyed by the finding's data_source."""
    floors: dict[str, Optional[str]] = {}
    for row in read_sources(with_health=False):
        severity = row.get("min_severity")
        if not isinstance(severity, str) or not severity:
            severity = None
        source_id = row.get("source_id")
        if not isinstance(source_id, str):
            continue
        floors[source_id] = severity
        floors[health_id(source_id)] = severity
        # A pulled source's findings use the integration id (elastic), while the
        # floor is stored on the federation id (elastic-siem).
        integration = LEGACY_POLLED.get(source_id)
        if isinstance(integration, str):
            floors[integration] = severity
    return floors


def _share(k: int, n: int) -> dict[str, Any]:
    """A count and its rate. Under 20 the rate is k of n. At 20, Wilson.

    A group with no alerts still carries a bound: Wilson's upper end when the
    source has other alerts, and the rule of three when it has none.
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
        bound = _ZERO_BOUND
    return {"k": k, "n": n, "kind": kind, "low": low, "high": high, "bound": bound}


def _span(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "window_start": _iso(start) or "",
        "window_end": _iso(end) or "",
        "window_length": _length(start, end),
    }


def _length(start: datetime, end: datetime) -> str:
    """The window's real length, in the same words as the page header."""
    minutes = max(0, int(round((end - start).total_seconds() / 60)))
    hours, mins = divmod(minutes, 60)
    if hours and mins == 0:
        return "1 hour" if hours == 1 else f"{hours} hours"
    if hours:
        return f"{hours} h {mins} min"
    return "1 min" if minutes == 1 else f"{minutes} min"


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _naive(value)
    if isinstance(value, str):
        return _parse(value)
    return None


def _shown(value: datetime) -> str:
    return f"{value.day} {value:%b} {value:%H:%M} UTC"
