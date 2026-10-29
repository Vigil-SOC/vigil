"""One read of the intake queue for the Triage screen.

One row per alert: its latest ``intake_triggers`` row, plus one ``not_picked_up``
row per finding that never got an intake row and is on no case (or is marked as
noise). An alert whose latest row is not queued and that is on no case is also
``not_picked_up``, as on Overview. Ask and schedule rows have no alert and are all
kept. An alert marked as noise is only ``dismissed``. ``counts`` cover all of
them, as SQL aggregates. Kind, source, and state narrow only the returned rows,
in SQL before the cap; only the rows returned are loaded and presented. Ranking
is ``rank_intake_row``; this module does not keep a second sort key.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    distinct,
    exists,
    func,
    literal,
    null,
    or_,
)
from sqlalchemy.orm import aliased

from core.federation.lag import source_collection_lag
from core.findings.arrival_counts import arrivals_today_by_source, utc_day_bounds
from core.findings.source_evidence import project_finding_source_evidence_for_list
from core.findings.source_link import resolve_source_link
from core.storage.connection import get_db_manager
from core.storage.models import (
    Case,
    Finding,
    IntakeTrigger,
    Investigation,
    case_findings,
)
from core.storage.shared_ioc_repository import CaseMatch
from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import _link_note, intake_rank_inputs, rank_intake_row

ROW_CAP = 200
ARRIVAL_INFO = (
    "Arrivals count every finding stored today. The list is one row per alert."
)
_PICKUP_STATES = ("launched", "merged")
# Not an intake state: an alert marked as noise is only this, whatever its intake
# state, so the Overview's "Dismissed as noise" count opens the queue at them.
NOISE_STATE = "dismissed"
# An alert on no case and not waiting, whatever its old rows say: Overview's "Not picked up".
NOT_PICKED_UP = "not_picked_up"
# An alert whose finding has no data_source counts and filters under this name.
UNKNOWN_SOURCE = "Unknown"
NOISE_LABEL = "Dismissed as noise"

ARRIVAL_INFO_ALL = "Arrivals count every finding stored. The list is one row per alert."
_TRUST_FLOOR_INFO = {
    "source": "Not recorded yet: Vigil keeps no per-source trust score.",
    "calculation": "The lowest score a finding needs before it is picked up automatically.",
    "limit": "No score is computed, so no floor can be shown.",
}
# The ⓘ copy for each strip figure, worded from what _strip counts, per range.
STRIP_INFO = {
    "picked_up": {
        "source": "Intake rows, all kinds, created today (UTC day).",
        "calculation": "Rows that launched a workflow or merged into a case, divided by rows created today.",
    },
    "waiting": {
        "source": "Alerts in the list that arrived today (UTC day) and are still queued.",
        "calculation": "A count of alerts in the Waiting state right now, noise-marked ones excluded.",
    },
    "cases_created_today": {
        "source": "Intake rows launched today (UTC day) that opened a case.",
        "calculation": "The number of distinct cases across those rows.",
    },
    "trust_floor": _TRUST_FLOOR_INFO,
}
STRIP_INFO_ALL = {
    "picked_up": {
        "source": "Intake rows, all kinds, from any day.",
        "calculation": "Rows that launched a workflow or merged into a case, divided by all rows.",
    },
    "waiting": {
        "source": "Alerts still queued, from any day.",
        "calculation": "A count of alerts in the Waiting state right now, noise-marked ones excluded.",
    },
    "cases_created_today": {
        "source": "Intake rows from any day that launched and opened a case.",
        "calculation": "The number of distinct cases across those rows.",
    },
    "trust_floor": _TRUST_FLOOR_INFO,
}

_KIND_LABELS = {
    "detection": "Alert",
    "schedule": "Schedule",
    "human_ask": "Ask",
}

# One sentence each for the figures a row's breakdown does not have yet.
BREAKDOWN_INFO = {
    "trust": "Vigil keeps no per-source trust score yet, so none is used in the ranking.",
    "weight": "Vigil keeps no per-row weight yet, so none is used in the ranking.",
    "score": "No score is computed, so there is nothing to compare against a floor.",
}


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.isoformat()


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _state_label(
    state: str,
    case_id: Optional[str],
    merged_into: Optional[str],
    case_ids: set[str],
) -> tuple[str, Optional[str]]:
    """Display word and the case door, when the id is still a case."""
    if state == "queued":
        return "Waiting for triage", None
    if state == "launched":
        if case_id:
            return "Started a case", case_id
        return "Picked up", None
    if state == "merged":
        # the door only when the id is still a case; never the id as the label
        return "Added to a case", merged_into if merged_into in case_ids else None
    if state == "expired":
        return "Expired", None
    if state == NOT_PICKED_UP:
        return "Not picked up", None
    return state, None


def _workflow(payload: dict, investigation_workflow: Optional[str]) -> str:
    raw = payload.get("workflow_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    if investigation_workflow:
        return investigation_workflow
    return ""


_DOCUMENT_KEYS = {"human_ask": "document", "schedule": "hypothesis"}


def _document_line(kind: str, payload: dict) -> Optional[str]:
    text = payload.get(_DOCUMENT_KEYS.get(kind))
    if not isinstance(text, str):
        return None
    line = " ".join(text.split())
    return line or None


def _ranking(row: dict, now: datetime, config: OrchestratorConfig) -> dict:
    ttl = config.intake_ttl_seconds
    band, promoted, age = intake_rank_inputs(
        row,
        now=now,
        ttl_seconds=ttl,
        promote_fraction=config.intake_ttl_promote_fraction,
    )
    return {
        "severity_band": band,
        "age_seconds": age,
        "ttl_seconds": ttl,
        "last_quarter": promoted,
    }


def _sources(day: Optional[date], now: datetime) -> list[dict]:
    """Arrivals in range (all time when ``day`` is None) already keep enabled sources. Lag only annotates those."""
    arrivals = {
        row["data_source"]: row["count"]
        for row in arrivals_today_by_source(day, all_time=day is None)
    }
    lags = {row["source_id"]: row for row in source_collection_lag(now)}
    sources = []
    for name in sorted(arrivals):
        lag = lags.get(name)
        sources.append(
            {
                "data_source": name,
                "arrivals": arrivals[name],
                "lag_seconds": None if lag is None else lag["lag_seconds"],
                "quiet": None if lag is None else lag["quiet"],
            }
        )
    return sources


def _finding_source(data_source):
    return func.coalesce(func.nullif(data_source, ""), literal(UNKNOWN_SOURCE))


def _source_column():
    """A row's source name, over IntakeTrigger outer-joined to Finding."""
    return case(
        (IntakeTrigger.kind == "schedule", literal("Schedule")),
        (IntakeTrigger.kind == "human_ask", literal("Ask")),
        else_=_finding_source(Finding.data_source),
    )


def _rows(session, day: Optional[date]):
    """Each alert's latest intake row and never-queued findings as one table, ``intake_id`` null for the latter.

    ``arrived_at`` is when the row's alert arrived (an Ask or schedule row's own
    creation); with ``day`` set only rows that arrived that UTC day are kept.
    """
    newer = aliased(IntakeTrigger)
    intake = (
        session.query(
            IntakeTrigger.id.label("intake_id"),
            IntakeTrigger.finding_id.label("finding_id"),
            IntakeTrigger.kind.label("kind"),
            # Not waiting and on no case reads as Overview's Not picked up.
            case(
                (
                    and_(
                        # a purged alert has no Finding row and keeps its old word
                        Finding.finding_id.isnot(None),
                        IntakeTrigger.state != "queued",
                        ~exists().where(
                            case_findings.c.finding_id == IntakeTrigger.finding_id
                        ),
                    ),
                    literal(NOT_PICKED_UP),
                ),
                else_=IntakeTrigger.state,
            ).label("state"),
            _source_column().label("source"),
            Finding.noise_marked_at.label("noise_marked_at"),
            IntakeTrigger.priority.label("priority"),
            Finding.severity.label("severity"),
            IntakeTrigger.created_at.label("created_at"),
            # decided rows sort newest first by this; a never-queued finding by its arrival
            IntakeTrigger.decided_at.label("order_at"),
            case(
                (
                    IntakeTrigger.kind == "detection",
                    func.coalesce(Finding.created_at, IntakeTrigger.created_at),
                ),
                else_=IntakeTrigger.created_at,
            ).label("arrived_at"),
        )
        .select_from(IntakeTrigger)
        .outerjoin(Finding, Finding.finding_id == IntakeTrigger.finding_id)
        # An alert's older rows stay in the table, not in the list.
        .filter(
            ~exists().where(
                newer.finding_id == IntakeTrigger.finding_id,
                newer.id > IntakeTrigger.id,
            )
        )
    )
    never_queued = session.query(
        cast(null(), Integer),
        Finding.finding_id,
        cast(literal("detection"), String),
        cast(literal(NOT_PICKED_UP), String),
        _finding_source(Finding.data_source),
        Finding.noise_marked_at,
        cast(null(), String),
        Finding.severity,
        Finding.created_at,
        Finding.created_at,
        Finding.created_at,
    ).filter(
        ~exists().where(IntakeTrigger.finding_id == Finding.finding_id),
        # A noise mark wins over a case: Overview counts it as dismissed.
        or_(
            ~exists().where(case_findings.c.finding_id == Finding.finding_id),
            Finding.noise_marked_at.isnot(None),
        ),
    )
    rows = intake.union_all(never_queued).subquery("all_triage_rows")
    query = session.query(rows)
    if day is not None:
        start, end = utc_day_bounds(day)
        query = query.filter(rows.c.arrived_at >= start, rows.c.arrived_at < end)
    return query.subquery("triage_rows")


def _filtered(
    query, rows, kind: Optional[str], source: Optional[str], state: Optional[str]
):
    if kind:
        query = query.filter(rows.c.kind == kind)
    if state == NOISE_STATE:
        query = query.filter(rows.c.noise_marked_at.isnot(None))
    elif state:
        query = query.filter(rows.c.state == state, rows.c.noise_marked_at.is_(None))
    if source:
        query = query.filter(rows.c.source == source)
    return query


RowKey = tuple[Optional[int], Optional[str]]


def _page_keys(
    session,
    rows,
    kind: Optional[str],
    source: Optional[str],
    state: Optional[str],
    now: datetime,
    config: OrchestratorConfig,
) -> tuple[list[RowKey], int]:
    """``(intake_id, finding_id)`` of the rows shown, in order, and how many rows match."""
    matched = _filtered(
        session.query(func.count()).select_from(rows), rows, kind, source, state
    ).scalar()
    # Every queued row, with only what rank_intake_row reads; never ORDER BY severity.
    queued = []
    for row_id, row_kind, priority, created, finding_id, severity in _filtered(
        session.query(
            rows.c.intake_id,
            rows.c.kind,
            rows.c.priority,
            rows.c.created_at,
            rows.c.finding_id,
            rows.c.severity,
        ).filter(rows.c.state == "queued"),
        rows,
        kind,
        source,
        state,
    ).order_by(rows.c.intake_id):
        row = {
            "id": row_id,
            "kind": row_kind,
            "priority": priority,
            "created_at": _naive(created),
            "finding_id": finding_id,
        }
        if finding_id is not None:
            row["_finding"] = {"severity": severity}
        queued.append(row)
    queued.sort(
        key=lambda row: rank_intake_row(
            row,
            now=now,
            ttl_seconds=config.intake_ttl_seconds,
            promote_fraction=config.intake_ttl_promote_fraction,
        )
    )
    keys: list[RowKey] = [(row["id"], row["finding_id"]) for row in queued[:ROW_CAP]]
    room = ROW_CAP - len(keys)
    if room > 0:
        # Newest first; a decided row with no decided_at sorts last.
        keys += [
            (row_id, finding_id)
            for row_id, finding_id in _filtered(
                session.query(rows.c.intake_id, rows.c.finding_id).filter(
                    rows.c.state != "queued"
                ),
                rows,
                kind,
                source,
                state,
            )
            .order_by(
                rows.c.order_at.desc().nullslast(),
                rows.c.intake_id.nullsfirst(),
                rows.c.finding_id,
            )
            .limit(room)
        ]
    return keys, matched


def _never_queued_id(finding_id: str) -> int:
    """A stable negative id for a row with no intake id; intake ids are positive."""
    digest = hashlib.sha256(finding_id.encode()).digest()
    # 48 bits stay a safe integer in the browser
    return -(int.from_bytes(digest[:6], "big") + 1)


def _load(
    session, keys: list[RowKey]
) -> tuple[list[dict], dict[str, dict], dict[str, str], set[str]]:
    """The shown rows in ``keys`` order, and only their findings, workflows, cases."""
    if not keys:
        return [], {}, {}, set()
    ids = [row_id for row_id, _finding_id in keys if row_id is not None]
    sources = {}
    by_id = {}
    if ids:
        for row, source in (
            session.query(IntakeTrigger, _source_column())
            .select_from(IntakeTrigger)
            .outerjoin(Finding, Finding.finding_id == IntakeTrigger.finding_id)
            .filter(IntakeTrigger.id.in_(ids))
        ):
            by_id[row.id] = row
            sources[row.id] = source
    triggers = [by_id[row_id] for row_id in ids if row_id in by_id]
    finding_ids = {row.finding_id for row in triggers if row.finding_id}
    finding_ids |= {fid for row_id, fid in keys if row_id is None and fid}
    findings: dict[str, dict] = {}
    if finding_ids:
        for finding in session.query(Finding).filter(
            Finding.finding_id.in_(finding_ids)
        ):
            findings[finding.finding_id] = {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "data_source": finding.data_source,
                "external_id": finding.external_id,
                "evidence_links": list(finding.evidence_links or []),
                "entity_context": finding.entity_context,
                "title": finding.title,
                "description": finding.description,
                "ai_enrichment": finding.ai_enrichment,
                "noise_marked_at": finding.noise_marked_at,
                "created_at": finding.created_at,
            }
    # Same rule as _rows: an alert not waiting and on no case reads as not picked up.
    on_case: set[str] = set()
    if finding_ids:
        on_case = {
            fid
            for (fid,) in session.query(case_findings.c.finding_id).filter(
                case_findings.c.finding_id.in_(finding_ids)
            )
        }
    inv_ids = {row.investigation_id for row in triggers if row.investigation_id}
    workflows: dict[str, str] = {}
    if inv_ids:
        for inv_id, workflow_id in (
            session.query(Investigation.investigation_id, Investigation.workflow_id)
            .filter(Investigation.investigation_id.in_(inv_ids))
            .all()
        ):
            workflows[inv_id] = workflow_id
    merged = {row.merged_into for row in triggers if row.merged_into}
    activities: dict[str, list] = {}
    if merged:
        activities = {
            case_id: acts or []
            for case_id, acts in session.query(Case.case_id, Case.activities)
            .filter(Case.case_id.in_(merged))
            .all()
        }
    case_ids = set(activities)
    rows = []
    for row_id, finding_id in keys:
        if row_id is None:
            finding = findings.get(finding_id or "")
            if finding is not None:
                rows.append(_never_queued_row(finding))
            continue
        row = by_id.get(row_id)
        if row is None:
            continue
        raw = row.payload if isinstance(row.payload, dict) else {}
        # The signed tool principal is for the worker, not the queue. A reader of
        # this row is not a holder of the internal token, and does not need it.
        payload = {key: value for key, value in raw.items() if key != "principal"}
        rows.append(
            {
                "id": row.id,
                "kind": row.kind,
                "state": (
                    NOT_PICKED_UP
                    if row.finding_id in findings
                    and row.state != "queued"
                    and row.finding_id not in on_case
                    else row.state
                ),
                "source": sources[row.id],
                "priority": row.priority,
                "finding_id": row.finding_id,
                "payload": payload,
                "investigation_id": row.investigation_id,
                "case_id": row.case_id,
                "merged_into": row.merged_into,
                "merge_note": _merge_note(
                    row, activities.get(row.merged_into or "", [])
                ),
                "created_at": _naive(row.created_at),
                "decided_at": _naive(row.decided_at),
            }
        )
    return rows, findings, workflows, case_ids


def _never_queued_row(finding: dict) -> dict:
    data_source = finding.get("data_source")
    return {
        "id": _never_queued_id(finding["finding_id"]),
        "kind": "detection",
        "state": NOT_PICKED_UP,
        "source": (
            data_source
            if isinstance(data_source, str) and data_source
            else UNKNOWN_SOURCE
        ),
        "priority": None,
        "finding_id": finding["finding_id"],
        "payload": {},
        "investigation_id": None,
        "case_id": None,
        "merged_into": None,
        "merge_note": None,
        "created_at": _naive(finding.get("created_at")),
        "decided_at": None,
    }


def _merge_note(row, case_activities: list) -> Optional[str]:
    """What a merged row matched, from the Case's ``alert_added`` activity; None when unknown."""
    if row.state != "merged":
        return None
    if row.reason == "finding_on_open_case":
        return "already on this case"
    if row.reason != "entity_on_open_case":
        return None
    for activity in case_activities:
        details = activity.get("details") if isinstance(activity, dict) else None
        if (
            isinstance(details, dict)
            and activity.get("activity_type") == "alert_added"
            and details.get("finding_id") == row.finding_id
            and details.get("matched_key")
        ):
            return _link_note(
                CaseMatch(
                    row.merged_into,
                    details["matched_key"],
                    bool(details.get("same_alert")),
                )
            )
    return None


def _evidence(finding: Optional[dict]) -> Optional[dict]:
    if not finding:
        return None
    projected = project_finding_source_evidence_for_list(
        {"entity_context": finding.get("entity_context")}
    )
    context = projected.get("entity_context")
    if not isinstance(context, dict):
        return None
    evidence = context.get("source_evidence")
    return evidence if isinstance(evidence, dict) else None


_VERDICT_SEVERITIES = ("critical", "high", "medium", "low")


def _triage(finding: Optional[dict]) -> dict:
    """What the daemon's triage saved for this alert, read as stored.

    ``done`` carries the saved severity, category, confidence and reasoning; a
    field triage did not give, or gave out of range, is null rather than a
    default. ``failed`` is a recorded error with no saved result. ``none`` is no
    result at all: not triaged yet, or it arrived while triage was off.
    """
    enrichment = (finding or {}).get("ai_enrichment")
    enrichment = enrichment if isinstance(enrichment, dict) else {}
    stored = enrichment.get("ai_triage")
    result = stored.get("result") if isinstance(stored, dict) else None
    if isinstance(result, dict) and result:
        severity = result.get("severity")
        category = result.get("category")
        confidence = result.get("confidence")
        reasoning = result.get("reasoning")
        when = stored.get("timestamp")
        return {
            "state": "done",
            "severity": severity if severity in _VERDICT_SEVERITIES else None,
            "category": category if isinstance(category, str) and category else None,
            "confidence": (
                float(confidence)
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and 0.0 <= confidence <= 1.0
                else None
            ),
            "reasoning": (
                reasoning if isinstance(reasoning, str) and reasoning else None
            ),
            "triaged_at": when if isinstance(when, str) else None,
        }
    error = enrichment.get("ai_triage_error")
    if isinstance(error, str) and error:
        return {"state": "failed", "error": error}
    return {"state": "none"}


def _present(
    row: dict,
    finding: Optional[dict],
    workflow: str,
    case_ids: set[str],
    now: datetime,
    config: OrchestratorConfig,
    link_configs: dict,
) -> dict:
    label, door = _state_label(
        row["state"], row.get("case_id"), row.get("merged_into"), case_ids
    )
    dismissed = bool(finding and finding.get("noise_marked_at"))
    if dismissed:
        label = NOISE_LABEL
    ranking = _ranking(row, now, config)
    created = row.get("created_at")
    decided = row.get("decided_at")
    pickup = None
    if (
        row["state"] in _PICKUP_STATES
        and isinstance(created, datetime)
        and isinstance(decided, datetime)
    ):
        pickup = (decided - created).total_seconds()
    kind = row["kind"]
    return {
        "id": row["id"],
        "kind": kind,
        "kind_label": _KIND_LABELS.get(kind, kind),
        "state": row["state"],
        "state_label": label,
        # the noise mark is the word shown, so the pill takes its tone
        "dismissed": dismissed,
        "source": row["source"],
        **ranking,
        "pickup_seconds": pickup,
        "workflow_id": workflow,
        "case_door": door,
        "merge_note": row.get("merge_note"),
        "document": _document_line(kind, row["payload"]),
        "source_link": (
            resolve_source_link(finding, configs=link_configs)
            if kind == "detection"
            else None
        ),
        "source_evidence": _evidence(finding) if kind == "detection" else None,
        "title": finding.get("title") if finding else None,
        "description": finding.get("description") if finding else None,
        "triage": _triage(finding) if kind == "detection" else None,
        "finding_id": row.get("finding_id"),
        "created_at": _iso(created if isinstance(created, datetime) else None),
        "decided_at": _iso(decided if isinstance(decided, datetime) else None),
    }


def _counts(session, rows) -> dict:
    """Every row by kind, source, and state, before filters and the cap.

    The states, with ``NOISE_STATE`` as one of them, split the rows with no
    overlap: a noise-marked row counts only under ``NOISE_STATE``.
    """
    kinds: dict[str, int] = {}
    sources: dict[str, int] = {}
    states: dict[str, int] = {}
    total = 0
    noisy = rows.c.noise_marked_at.isnot(None)
    for kind, state, name, is_noise, n in session.query(
        rows.c.kind, rows.c.state, rows.c.source, noisy, func.count()
    ).group_by(rows.c.kind, rows.c.state, rows.c.source, noisy):
        total += n
        kinds[kind] = kinds.get(kind, 0) + n
        state = NOISE_STATE if is_noise else state
        states[state] = states.get(state, 0) + n
        sources[name] = sources.get(name, 0) + n
    return {"total": total, "kind": kinds, "source": sources, "state": states}


def _strip(session, counts: dict, day: Optional[date]) -> dict:
    """The strip for one UTC day, or every row when ``day`` is None."""
    picked = session.query(
        func.count(IntakeTrigger.id),
        func.count(IntakeTrigger.id).filter(IntakeTrigger.state.in_(_PICKUP_STATES)),
    )
    cases = session.query(func.count(distinct(IntakeTrigger.case_id))).filter(
        IntakeTrigger.state == "launched",
        IntakeTrigger.case_id.isnot(None),
        IntakeTrigger.case_id != "",
    )
    if day is not None:
        start, end = utc_day_bounds(day)
        picked = picked.filter(
            IntakeTrigger.created_at >= start, IntakeTrigger.created_at < end
        )
        cases = cases.filter(
            IntakeTrigger.decided_at >= start, IntakeTrigger.decided_at < end
        )
    denominator, numerator = picked.one()
    cases = cases.scalar()
    return {
        "picked_up": {
            "launched_or_merged": numerator,
            "created_today": denominator,
            "share": None if denominator == 0 else numerator / denominator,
        },
        "waiting": counts["state"].get("queued", 0),
        "cases_created_today": cases,
    }


def _finding_view(finding: dict) -> dict:
    data_source = finding.get("data_source")
    title = finding.get("title")
    description = finding.get("description")
    return {
        "finding_id": finding.get("finding_id"),
        "severity": finding.get("severity"),
        "data_source": data_source if isinstance(data_source, str) else "",
        "external_id": finding.get("external_id"),
        "evidence_links": list(finding.get("evidence_links") or []),
        "entity_context": finding.get("entity_context"),
        "title": title if isinstance(title, str) else None,
        "description": description if isinstance(description, str) else None,
        "ai_enrichment": finding.get("ai_enrichment"),
    }


def _demo_triage(
    findings: list[dict],
    cases: list[dict],
    *,
    kind: Optional[str],
    source: Optional[str],
    state: Optional[str],
    day: Optional[date],
    now: datetime,
) -> dict:
    """One detection row per demo finding. A linked case counts as launched; no case is not picked up.

    With ``day`` set only findings that arrived that UTC day are rows.
    """
    from core.findings.demo_reads import (
        _noise,
        _ord,
        _when,
        best_case_by_finding,
        parse_utc,
    )
    from services.daemon.orchestrator import rank_intake_row

    linked = best_case_by_finding(cases)
    case_ids = {
        case_id
        for case in cases
        if isinstance((case_id := case.get("case_id")), str) and case_id
    }
    config = OrchestratorConfig()
    built: list[dict] = []
    for index, finding in enumerate(
        sorted(findings, key=lambda row: row.get("finding_id") or ""), start=1
    ):
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str):
            continue
        case = linked.get(finding_id)
        created = _when(finding)
        if day is not None and (created is None or created.date() != day):
            continue
        decided = parse_utc(case.get("created_at")) if case else None
        view = _finding_view(finding)
        view["noise_marked_at"] = _noise(finding)
        case_id = case.get("case_id") if case else None
        built.append(
            {
                "id": index,
                "kind": "detection",
                "state": "launched" if case else NOT_PICKED_UP,
                "source": view["data_source"] or UNKNOWN_SOURCE,
                "priority": None,
                "finding_id": finding_id,
                "payload": {},
                "investigation_id": None,
                "case_id": case_id if isinstance(case_id, str) else None,
                "merged_into": None,
                "created_at": created,
                "decided_at": decided,
                "_finding": view,
            }
        )

    counts_kind: dict[str, int] = {}
    counts_source: dict[str, int] = {}
    counts_state: dict[str, int] = {}
    noise = {f.get("finding_id") for f in findings if _noise(f)}
    for row in built:
        row_kind = str(row["kind"])
        # A noise-marked alert counts only as dismissed.
        row_state = NOISE_STATE if row["finding_id"] in noise else str(row["state"])
        counts_kind[row_kind] = counts_kind.get(row_kind, 0) + 1
        counts_state[row_state] = counts_state.get(row_state, 0) + 1
        counts_source[row["source"]] = counts_source.get(row["source"], 0) + 1

    def _at(row: dict, field: str) -> Optional[datetime]:
        value = row.get(field)
        return value if isinstance(value, datetime) else None

    # rows are already in range; under today a case also counts only when opened that day
    launched_today = sum(1 for row in built if row["state"] in _PICKUP_STATES)
    cases_today = {
        row["case_id"]
        for row in built
        if row["state"] == "launched"
        and isinstance(row["case_id"], str)
        and (
            day is None
            or ((stamp := _at(row, "decided_at")) is not None and stamp.date() == day)
        )
    }
    arrivals: dict[str, int] = {}
    for row in built:
        arrivals[row["source"]] = arrivals.get(row["source"], 0) + 1

    selected = [
        row
        for row in built
        if (not kind or row["kind"] == kind)
        and (
            not state
            or (
                row["finding_id"] in noise
                if state == NOISE_STATE
                else row["finding_id"] not in noise and row["state"] == state
            )
        )
        and (not source or row["source"] == source)
    ]
    queued = [row for row in selected if row["state"] == "queued"]
    queued.sort(
        key=lambda row: rank_intake_row(
            row,
            now=now,
            ttl_seconds=config.intake_ttl_seconds,
            promote_fraction=config.intake_ttl_promote_fraction,
        )
    )
    decided_rows = [row for row in selected if row["state"] != "queued"]
    # a not-picked-up row has no decision, so it sorts by its arrival, as live
    decided_rows.sort(
        key=lambda row: (
            -_ord(_at(row, "decided_at") or _at(row, "created_at")),
            row["id"],
        )
    )
    shown = (queued + decided_rows)[:ROW_CAP]
    link_configs: dict[str, dict[str, str]] = {}
    presented = [
        _present(row, row["_finding"], "", case_ids, now, config, link_configs)
        for row in shown
    ]
    denominator = len(built)
    return {
        "rows": presented,
        "matched": len(selected),
        "strip": {
            "picked_up": {
                "launched_or_merged": launched_today,
                "created_today": denominator,
                "share": None if denominator == 0 else launched_today / denominator,
            },
            "waiting": counts_state.get("queued", 0),
            "cases_created_today": len(cases_today),
        },
        "counts": {
            "total": len(built),
            "kind": counts_kind,
            "source": counts_source,
            "state": counts_state,
        },
        "sources": [
            {
                "data_source": name,
                "arrivals": arrivals[name],
                "lag_seconds": None,
                "quiet": None,
            }
            for name in sorted(arrivals)
        ],
        "range": "today" if day is not None else "all",
        "arrival_info": ARRIVAL_INFO if day is not None else ARRIVAL_INFO_ALL,
        "strip_info": STRIP_INFO if day is not None else STRIP_INFO_ALL,
        "breakdown_info": BREAKDOWN_INFO,
    }


def triage_payload(
    *,
    kind: Optional[str] = None,
    source: Optional[str] = None,
    state: Optional[str] = None,
    day: Optional[date] = None,
    now: Optional[datetime] = None,
    range: str = "today",
) -> dict:
    """Queued rows by ``rank_intake_row``, then decided rows newest first.

    ``range="today"`` keeps rows that arrived on ``day`` (the UTC day by
    default); ``"all"`` keeps every row.
    """
    if now is None:
        now = utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if day is None:
        day = now.date()
    bound: Optional[date] = None if range == "all" else day
    from core.config import is_demo_mode

    if is_demo_mode():
        from core.findings.demo_reads import load_demo

        findings, cases = load_demo()
        return _demo_triage(
            findings, cases, kind=kind, source=source, state=state, day=bound, now=now
        )
    config = OrchestratorConfig()
    with get_db_manager().session_scope() as session:
        # One snapshot for every read: a row claimed between the queued and
        # decided queries would otherwise be listed twice.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        rows = _rows(session, bound)
        counts = _counts(session, rows)
        strip = _strip(session, counts, bound)
        keys, matched = _page_keys(session, rows, kind, source, state, now, config)
        shown, findings, workflows, case_ids = _load(session, keys)
    link_configs: dict[str, dict[str, str]] = {}
    presented = []
    for row in shown:
        finding = findings.get(row["finding_id"]) if row.get("finding_id") else None
        if finding is not None:
            row["_finding"] = finding
        workflow = _workflow(
            row["payload"], workflows.get(row.get("investigation_id") or "")
        )
        presented.append(
            _present(row, finding, workflow, case_ids, now, config, link_configs)
        )
    return {
        "rows": presented,
        "matched": matched,
        "strip": strip,
        "counts": counts,
        "sources": _sources(bound, now),
        "range": range,
        "arrival_info": ARRIVAL_INFO if bound else ARRIVAL_INFO_ALL,
        "strip_info": STRIP_INFO if bound else STRIP_INFO_ALL,
        "breakdown_info": BREAKDOWN_INFO,
    }
