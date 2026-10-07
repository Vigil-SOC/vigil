"""One read of the intake queue for the Triage screen.

The strip counts the whole ``intake_triggers`` table. Kind, source, and state
narrow only the returned rows. Ranking is ``rank_intake_row``; this module
does not keep a second sort key.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

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

_KIND_LABELS = {
    "detection": "Alert",
    "schedule": "Schedule",
    "human_ask": "Ask",
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
        return "Waiting", None
    if state == "launched":
        if case_id:
            return "Started a case", case_id
        return "Picked up", None
    if state == "merged":
        if merged_into and merged_into in case_ids:
            return "Added to a case", merged_into
        return merged_into or "", None
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


def _load() -> tuple[list[dict], dict[str, dict], dict[str, str], set[str]]:
    db = get_db_manager()
    with db.session_scope() as session:
        triggers = session.query(IntakeTrigger).all()
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
        "description": finding.get("description") if finding else None,
        "finding_id": row.get("finding_id"),
        "created_at": _iso(created if isinstance(created, datetime) else None),
        "decided_at": _iso(decided if isinstance(decided, datetime) else None),
    }


def _strip(rows: list[dict], day: date) -> dict:
    start, end = utc_day_bounds(day)
    created_today = [
        row
        for row in rows
        if isinstance(row.get("created_at"), datetime)
        and start <= row["created_at"] < end
    ]
    denominator = len(created_today)
    numerator = sum(
        1 for row in created_today if row["state"] in ("launched", "merged")
    )
    cases = {
        row["case_id"]
        for row in rows
        if row["state"] == "launched"
        and row.get("case_id")
        and isinstance(row.get("decided_at"), datetime)
        and start <= row["decided_at"] < end
    }
    return {
        "picked_up": {
            "launched_or_merged": numerator,
            "created_today": denominator,
            "share": None if denominator == 0 else numerator / denominator,
        },
        "waiting": sum(1 for row in rows if row["state"] == "queued"),
        "cases_created_today": len(cases),
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
    stored, findings, workflows, case_ids = _load()
    for row in stored:
        finding = findings.get(row["finding_id"]) if row.get("finding_id") else None
        if finding is not None:
            row["_finding"] = finding
    strip = _strip(stored, day)
    queued = [row for row in stored if row["state"] == "queued"]
    decided = [row for row in stored if row["state"] != "queued"]
    queued.sort(
        key=lambda row: rank_intake_row(
            row,
            now=now,
            ttl_seconds=config.intake_ttl_seconds,
            promote_fraction=config.intake_ttl_promote_fraction,
        )
    )
    decided.sort(
        key=lambda row: (
            row["decided_at"]
            if isinstance(row.get("decided_at"), datetime)
            else datetime.min
        ),
        reverse=True,
    )
    link_configs: dict[str, dict[str, str]] = {}
    presented = []
    for row in queued + decided:
        finding = findings.get(row["finding_id"]) if row.get("finding_id") else None
        workflow = _workflow(
            row["payload"], workflows.get(row.get("investigation_id") or "")
        )
        item = _present(row, finding, workflow, case_ids, now, config, link_configs)
        if kind and item["kind"] != kind:
            continue
        if state and item["state"] != state:
            continue
        if source and item["source"] != source:
            continue
        presented.append(item)
        if len(presented) >= ROW_CAP:
            break
    return {
        "rows": presented,
        "strip": strip,
        "sources": _sources(day, now),
        "arrival_info": ARRIVAL_INFO,
        "unmeasured_text": UNMEASURED,
    }
