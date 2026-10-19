"""One read of the intake queue for the Triage screen.

The strip and ``counts`` cover the whole ``intake_triggers`` table, as SQL
aggregates. Kind, source, and state narrow only the returned rows, in SQL before
the cap; only the rows returned are loaded and presented. Ranking is
``rank_intake_row``; this module does not keep a second sort key.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import case, distinct, func, literal

from core.federation.lag import source_collection_lag
from core.findings.arrival_counts import arrivals_today_by_source, utc_day_bounds
from core.findings.source_evidence import project_finding_source_evidence_for_list
from core.findings.source_link import resolve_source_link
from core.storage.connection import get_db_manager
from core.storage.models import Case, Finding, IntakeTrigger, Investigation
from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import (
    intake_age_seconds,
    intake_severity_band,
    rank_intake_row,
)

ROW_CAP = 200
UNMEASURED = "Not measured yet"
ARRIVAL_INFO = "Arrivals count every finding stored today. The list is the intake rows."
_PICKUP_STATES = ("launched", "merged")

# The ⓘ copy for each strip figure, worded from what _strip counts.
STRIP_INFO = {
    "picked_up": {
        "source": "Intake rows, all kinds, created today (UTC day).",
        "calculation": "Rows that launched a workflow or merged into a case, divided by rows created today.",
    },
    "waiting": {
        "source": "Intake rows still queued, from any day.",
        "calculation": "A count of rows in the Waiting state right now.",
    },
    "cases_created_today": {
        "source": "Intake rows launched today (UTC day) that opened a case.",
        "calculation": "The number of distinct cases across those rows.",
    },
    "trust_floor": {
        "source": "Not recorded yet: Vigil keeps no per-source trust score.",
        "calculation": "The lowest score a finding needs before it is picked up automatically.",
        "limit": "No score is computed, so no floor can be shown.",
    },
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


def _source_text(kind: str, finding: Optional[dict]) -> str:
    if kind == "schedule":
        return "Schedule"
    if kind == "human_ask":
        return "Ask"
    if finding and finding.get("data_source"):
        return str(finding["data_source"])
    return ""


def _state_label(
    state: str,
    case_id: Optional[str],
    merged_into: Optional[str],
    case_ids: set[str],
) -> tuple[str, Optional[str]]:
    """Display word and the case door, when the id is still a case."""
    if state == "queued":
        return "Waiting for a slot", None
    if state == "launched":
        if case_id:
            return "Started a case", case_id
        return "Picked up", None
    if state == "merged":
        # the door only when the id is still a case; never the id as the label
        return "Added to a case", merged_into if merged_into in case_ids else None
    if state == "expired":
        return "Expired", None
    return state, None


def _workflow(payload: dict, investigation_workflow: Optional[str]) -> str:
    raw = payload.get("workflow_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    if investigation_workflow:
        return investigation_workflow
    return ""


def _document_line(kind: str, payload: dict) -> Optional[str]:
    if kind != "human_ask":
        return None
    document = payload.get("document")
    if not isinstance(document, str):
        return None
    line = " ".join(document.split())
    return line or None


def _ranking(row: dict, now: datetime, config: OrchestratorConfig) -> dict:
    """The inputs ``rank_intake_row`` sorts on, computed the same way."""
    age = intake_age_seconds(row, now)
    ttl = config.intake_ttl_seconds
    remaining = ttl - age
    finding = row.get("_finding")
    finding_severity = finding.get("severity") if isinstance(finding, dict) else None
    band = intake_severity_band(
        row.get("kind"),
        finding_severity=finding_severity,
        priority=row.get("priority"),
    )
    return {
        "severity_band": band,
        "age_seconds": age,
        "ttl_seconds": ttl,
        "last_quarter": 0 < remaining <= ttl * config.intake_ttl_promote_fraction,
    }


def _sources(day: date, now: datetime) -> list[dict]:
    """Arrivals already keep enabled sources. Lag only annotates those."""
    arrivals = {
        row["data_source"]: row["count"] for row in arrivals_today_by_source(day)
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


def _source_column():
    """``_source_text`` in SQL, over IntakeTrigger outer-joined to Finding."""
    return case(
        (IntakeTrigger.kind == "schedule", literal("Schedule")),
        (IntakeTrigger.kind == "human_ask", literal("Ask")),
        else_=func.coalesce(Finding.data_source, ""),
    )


def _joined(session, *columns):
    return (
        session.query(*columns)
        .select_from(IntakeTrigger)
        .outerjoin(Finding, Finding.finding_id == IntakeTrigger.finding_id)
    )


def _filtered(query, kind: Optional[str], source: Optional[str], state: Optional[str]):
    if kind:
        query = query.filter(IntakeTrigger.kind == kind)
    if state:
        query = query.filter(IntakeTrigger.state == state)
    if source:
        query = query.filter(_source_column() == source)
    return query


def _page_ids(
    session,
    kind: Optional[str],
    source: Optional[str],
    state: Optional[str],
    now: datetime,
    config: OrchestratorConfig,
) -> tuple[list[int], int]:
    """The ids of the rows shown, in order, and how many rows match."""
    matched = _filtered(
        _joined(session, func.count(IntakeTrigger.id)), kind, source, state
    ).scalar()
    # Every queued row, with only what rank_intake_row reads; never ORDER BY severity.
    queued = []
    for row_id, row_kind, priority, created, finding_id, severity in _filtered(
        _joined(
            session,
            IntakeTrigger.id,
            IntakeTrigger.kind,
            IntakeTrigger.priority,
            IntakeTrigger.created_at,
            Finding.finding_id,
            Finding.severity,
        ).filter(IntakeTrigger.state == "queued"),
        kind,
        source,
        state,
    ).order_by(IntakeTrigger.id):
        row = {
            "id": row_id,
            "kind": row_kind,
            "priority": priority,
            "created_at": _naive(created),
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
    ids = [row["id"] for row in queued[:ROW_CAP]]
    room = ROW_CAP - len(ids)
    if room > 0:
        # Newest decided first; a row with no decided_at sorts last.
        ids += [
            row_id
            for (row_id,) in _filtered(
                _joined(session, IntakeTrigger.id).filter(
                    IntakeTrigger.state != "queued"
                ),
                kind,
                source,
                state,
            )
            .order_by(IntakeTrigger.decided_at.desc().nullslast(), IntakeTrigger.id)
            .limit(room)
        ]
    return ids, matched


def _load(
    session, ids: list[int]
) -> tuple[list[dict], dict[str, dict], dict[str, str], set[str]]:
    """The shown rows in ``ids`` order, and only their findings, workflows, cases."""
    if not ids:
        return [], {}, {}, set()
    by_id = {
        row.id: row
        for row in session.query(IntakeTrigger).filter(IntakeTrigger.id.in_(ids))
    }
    triggers = [by_id[row_id] for row_id in ids if row_id in by_id]
    finding_ids = {row.finding_id for row in triggers if row.finding_id}
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
    case_ids: set[str] = set()
    if merged:
        case_ids = {
            case_id
            for (case_id,) in session.query(Case.case_id)
            .filter(Case.case_id.in_(merged))
            .all()
        }
    rows = []
    for row in triggers:
        payload = row.payload if isinstance(row.payload, dict) else {}
        rows.append(
            {
                "id": row.id,
                "kind": row.kind,
                "state": row.state,
                "priority": row.priority,
                "finding_id": row.finding_id,
                "payload": payload,
                "investigation_id": row.investigation_id,
                "case_id": row.case_id,
                "merged_into": row.merged_into,
                "created_at": _naive(row.created_at),
                "decided_at": _naive(row.decided_at),
            }
        )
    return rows, findings, workflows, case_ids


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
        "source": _source_text(kind, finding),
        **ranking,
        "score": None,
        "trust": None,
        "weight": None,
        "pickup_seconds": pickup,
        "workflow_id": workflow,
        "case_door": door,
        "document": _document_line(kind, row["payload"]),
        "source_link": (
            resolve_source_link(finding, configs=link_configs)
            if kind == "detection"
            else None
        ),
        "source_evidence": _evidence(finding) if kind == "detection" else None,
        "title": finding.get("title") if finding else None,
        "description": finding.get("description") if finding else None,
        "finding_id": row.get("finding_id"),
        "created_at": _iso(created if isinstance(created, datetime) else None),
        "decided_at": _iso(decided if isinstance(decided, datetime) else None),
    }


def _counts(session) -> dict:
    """Every intake row by kind, source, and state, before filters and the cap."""
    kinds: dict[str, int] = {}
    sources: dict[str, int] = {}
    states: dict[str, int] = {}
    total = 0
    source = _source_column()
    for kind, state, name, n in _joined(
        session, IntakeTrigger.kind, IntakeTrigger.state, source, func.count()
    ).group_by(IntakeTrigger.kind, IntakeTrigger.state, source):
        total += n
        kinds[kind] = kinds.get(kind, 0) + n
        states[state] = states.get(state, 0) + n
        if name:
            sources[name] = sources.get(name, 0) + n
    return {"total": total, "kind": kinds, "source": sources, "state": states}


def _strip(session, counts: dict, day: date) -> dict:
    start, end = utc_day_bounds(day)
    denominator, numerator = (
        session.query(
            func.count(IntakeTrigger.id),
            func.count(IntakeTrigger.id).filter(
                IntakeTrigger.state.in_(_PICKUP_STATES)
            ),
        )
        .filter(IntakeTrigger.created_at >= start, IntakeTrigger.created_at < end)
        .one()
    )
    cases = (
        session.query(func.count(distinct(IntakeTrigger.case_id)))
        .filter(
            IntakeTrigger.state == "launched",
            IntakeTrigger.case_id.isnot(None),
            IntakeTrigger.case_id != "",
            IntakeTrigger.decided_at >= start,
            IntakeTrigger.decided_at < end,
        )
        .scalar()
    )
    return {
        "picked_up": {
            "launched_or_merged": numerator,
            "created_today": denominator,
            "share": None if denominator == 0 else numerator / denominator,
        },
        "waiting": counts["state"].get("queued", 0),
        "cases_created_today": cases,
        "trust_floor": UNMEASURED,
    }


def triage_payload(
    *,
    kind: Optional[str] = None,
    source: Optional[str] = None,
    state: Optional[str] = None,
    day: Optional[date] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Queued rows by ``rank_intake_row``, then decided rows newest first."""
    if now is None:
        now = utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if day is None:
        day = now.date()
    config = OrchestratorConfig()
    with get_db_manager().session_scope() as session:
        # One snapshot for every read: a row claimed between the queued and
        # decided queries would otherwise be listed twice.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        counts = _counts(session)
        strip = _strip(session, counts, day)
        ids, matched = _page_ids(session, kind, source, state, now, config)
        shown, findings, workflows, case_ids = _load(session, ids)
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
        "sources": _sources(day, now),
        "arrival_info": ARRIVAL_INFO,
        "strip_info": STRIP_INFO,
        "breakdown_info": BREAKDOWN_INFO,
        "unmeasured_text": UNMEASURED,
    }
