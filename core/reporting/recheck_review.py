"""The blind review of a capped re-check, and the Case opened from it (#5095).

The queue is a read of ``capped_recheck_tickets`` for a batch that is
``complete`` or ``stopped: …``. Keys are the ones the batch stored. A ticket
Vigil left undetermined, or that is still in flight, is not queued. Reviews
are inserts. The first one is the call that counts.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import BigInteger, String, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.cases.case_sla_service import case_title, open_case
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Case, HistoryAlert, RecheckReview, WorkflowRun
from core.workflows.redecision import _content, redecision_result

REVIEWED = "recheck.reviewed"
REMOVED = "removed with its import"
ERASED = "evidence erased with its import"
CALLS = ("real", "not real", "can't tell")

_DRAWN = text("""
    SELECT t.id, t.batch_id, t.import_id, t.source, t.ticket_id, t.random_key,
           t.run_id, t.reason, b.created_at AS batch_created,
           h.category, h.closed_at, h.opened_at, h.title, h.severity,
           h.rule_id, h.entity_keys, h.alert_ids,
           (i.id IS NOT NULL) AS import_present
    FROM capped_recheck_tickets t
    JOIN capped_recheck_batches b ON b.batch_id = t.batch_id
    LEFT JOIN LATERAL (
        SELECT category, closed_at, opened_at, title, severity, rule_id,
               entity_keys, alert_ids
        FROM history_tickets h
        WHERE h.import_id = t.import_id AND h.ticket_id = t.ticket_id
        ORDER BY h.position
        LIMIT 1
    ) h ON true
    LEFT JOIN history_imports i ON i.id = t.import_id
    WHERE b.status = 'complete' OR b.status LIKE 'stopped:%'
    """)

_TERMINALS = text("""
    SELECT e.run_id::text AS run_id, e.payload
    FROM agent_events e
    WHERE e.kind = 'terminal' AND e.run_id::text = ANY(:ids)
    """).bindparams(bindparam("ids", type_=ARRAY(String())))

_TOOL_CALLS = text("""
    SELECT payload
    FROM agent_events
    WHERE kind = 'call' AND run_id::text = :run_id
    ORDER BY seq
    """)

_REVIEWS = text("""
    SELECT ticket_id, call, actor
    FROM recheck_reviews
    WHERE ticket_id = ANY(:ids)
    ORDER BY id
    """).bindparams(bindparam("ids", type_=ARRAY(BigInteger())))

_CASES = text("""
    SELECT case_id, recheck_mark->>'run_id' AS run_id
    FROM cases
    WHERE recheck_mark->>'run_id' = ANY(:ids)
    """).bindparams(bindparam("ids", type_=ARRAY(String())))


@dataclass
class _Ticket:
    id: int
    batch_id: str
    import_id: str
    source: str
    ticket_id: str
    random_key: str
    run_id: str
    batch_created: Any
    closed_at: Any
    opened_at: Any
    title: str
    severity: Any
    rule_id: Any
    entity_keys: List[str]
    alert_ids: List[str]
    vigil: Dict[str, Any]
    closure: str


class NotQueued(LookupError):
    """This ticket is not in a finished batch's review queue."""


class Removed(Exception):
    """The import is gone. ``notice`` is the sentence the reader shows."""

    def __init__(self, notice: str = REMOVED) -> None:
        self.notice = notice
        super().__init__(notice)


class CannotOpen(Exception):
    """This confirmation does not open a Case."""


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else str(value)


def closure_verdict(category: Optional[str]) -> Optional[str]:
    """``history_tickets.category`` as a review verdict.

    ``resolved`` is real, ``benign`` and ``false_positive`` are not real, and
    ``unable_to_resolve`` is undetermined. Anything else is not a side.
    """
    if category == "resolved":
        return "real"
    if category in ("benign", "false_positive"):
        return "not real"
    if category == "unable_to_resolve":
        return "undetermined"
    return None


def _vigil(payload: Any) -> Optional[Dict[str, Any]]:
    """A decisive re-decision, or None when it is undetermined or unfinished."""
    if not isinstance(payload, dict):
        return None
    mapped = redecision_result(
        str(payload.get("outcome") or ""),
        disposition=(
            payload.get("disposition")
            if isinstance(payload.get("disposition"), str)
            else None
        ),
        reason=str(payload.get("reason") or ""),
        proposed_actions=(
            payload.get("proposed_actions")
            if isinstance(payload.get("proposed_actions"), list)
            else []
        ),
    )
    result = mapped["result"]
    if result not in ("real", "not real"):
        return None
    summary = payload.get("summary")
    return {
        "verdict": result,
        "summary": summary if isinstance(summary, str) else "",
        "proposed_actions": mapped["proposed_actions"],
    }


def _bucket(closure: str, vigil: str) -> str:
    if closure == "undetermined" and vigil == "not real":
        return "undetermined_not_real"
    if closure == vigil:
        return "agreement"
    return "always"


def _take(rows: Sequence[Any]) -> List[Any]:
    """The lowest stored keys: all of a short group, else max(5, ceil(n/5))."""
    ordered = sorted(rows, key=lambda row: row.random_key)
    count = len(ordered)
    if count < 5:
        return list(ordered)
    return list(ordered[: max(5, math.ceil(count / 5))])


def _uuid(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _terminals(session: Session, run_ids: Sequence[str]) -> Dict[str, Any]:
    ids = [run_id for run_id in run_ids if _uuid(run_id)]
    if not ids:
        return {}
    rows = session.execute(_TERMINALS, {"ids": ids})
    return {row.run_id: row.payload for row in rows}


def _review_rows(session: Session, ticket_ids: Sequence[int]) -> Dict[int, List[Any]]:
    if not ticket_ids:
        return {}
    grouped: Dict[int, List[Any]] = {}
    for row in session.execute(_REVIEWS, {"ids": list(ticket_ids)}):
        grouped.setdefault(int(row.ticket_id), []).append(row)
    return grouped


def _case_by_run(session: Session, run_ids: Sequence[str]) -> Dict[str, str]:
    ids = [run_id for run_id in run_ids if run_id]
    if not ids:
        return {}
    return {row.run_id: row.case_id for row in session.execute(_CASES, {"ids": ids})}


def _members(session: Session) -> List[Any]:
    """Queue rows for every finished or stopped batch. Imports that are gone drop out."""
    drawn = list(session.execute(_DRAWN))
    terminals = _terminals(
        session,
        [
            row.run_id
            for row in drawn
            if row.run_id and row.import_present and not row.reason
        ],
    )
    kept: Dict[str, List[_Ticket]] = {}
    for row in drawn:
        if not row.import_present or row.reason or not row.run_id:
            continue
        vigil = _vigil(terminals.get(row.run_id))
        closure = closure_verdict(row.category)
        if vigil is None or closure is None:
            continue
        ticket = _Ticket(
            id=int(row.id),
            batch_id=row.batch_id,
            import_id=row.import_id,
            source=row.source,
            ticket_id=row.ticket_id,
            random_key=row.random_key,
            run_id=row.run_id,
            batch_created=row.batch_created,
            closed_at=row.closed_at,
            opened_at=row.opened_at,
            title=row.title or "",
            severity=row.severity,
            rule_id=row.rule_id,
            entity_keys=list(row.entity_keys or []),
            alert_ids=list(row.alert_ids or []),
            vigil=vigil,
            closure=closure,
        )
        kept.setdefault(row.batch_id, []).append(ticket)

    chosen: List[_Ticket] = []
    for rows in kept.values():
        always, agreement, undetermined = [], [], []
        for ticket in rows:
            bucket = _bucket(ticket.closure, ticket.vigil["verdict"])
            if bucket == "agreement":
                agreement.append(ticket)
            elif bucket == "undetermined_not_real":
                undetermined.append(ticket)
            else:
                always.append(ticket)
        chosen.extend(always)
        chosen.extend(_take(agreement))
        chosen.extend(_take(undetermined))
    # Newest batch first. Within a batch, the lower stored key stays first.
    chosen.sort(key=lambda ticket: ticket.random_key)
    chosen.sort(key=lambda ticket: ticket.batch_created, reverse=True)
    return chosen


def _may_open(
    row: Any, reviews: Sequence[Any], actor: Optional[str], case_id: Optional[str]
) -> bool:
    if case_id or not reviews or not actor:
        return False
    first = reviews[0]
    if first.actor != actor or first.call != "real":
        return False
    # A closure left undetermined does not open a Case, even when confirmed.
    if row.closure == "undetermined":
        return False
    if row.closure == "not real" and row.vigil["verdict"] == "real":
        return True
    return row.closure == "not real" and row.vigil["verdict"] == "not real"


def _differ(reviews: Sequence[Any]) -> bool:
    if len(reviews) < 2:
        return False
    first = reviews[0].call
    return any(row.call != first for row in reviews[1:])


def _summary(
    row: Any, reviews: Sequence[Any], actor: Optional[str], case_id: Optional[str]
) -> Dict[str, Any]:
    recorded = bool(reviews)
    return {
        "id": int(row.id),
        "batch_id": row.batch_id,
        "source": row.source,
        "closed_at": _iso(row.closed_at),
        "ticket_id": row.ticket_id,
        "title": row.title or "",
        "recorded": recorded,
        "call": reviews[0].call if recorded else None,
        "reviewers_differ": _differ(reviews),
        "can_open": _may_open(row, reviews, actor, case_id),
        "case_id": case_id,
    }


def queue(session: Session, *, actor: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Unconfirmed and confirmed, apart. A gone import is in neither."""
    rows = _members(session)
    reviews = _review_rows(session, [int(row.id) for row in rows])
    cases = _case_by_run(session, [row.run_id for row in rows])
    pending: List[Dict[str, Any]] = []
    reviewed: List[Dict[str, Any]] = []
    for row in rows:
        calls = reviews.get(int(row.id), [])
        item = _summary(row, calls, actor, cases.get(row.run_id))
        (reviewed if calls else pending).append(item)
    return {"pending": pending, "reviewed": reviewed}


def queued_count(session: Session, import_id: str) -> int:
    """How many queued tickets this import's delete will take off the queue."""
    return sum(1 for row in _members(session) if row.import_id == import_id)


def _row(session: Session, ticket_id: int) -> Any:
    drawn = list(session.execute(_DRAWN))
    match = next((row for row in drawn if int(row.id) == ticket_id), None)
    if match is None:
        raise NotQueued(ticket_id)
    # Import gone. No run row means the ledger was erased. A row that is
    # still decisive left the queue with the import, and a reason means
    # the claim was refused before a run existed.
    if not match.import_present and match.run_id and not match.reason:
        if session.get(WorkflowRun, match.run_id) is None:
            raise Removed(ERASED)
        vigil = _vigil(_terminals(session, [match.run_id]).get(match.run_id))
        if vigil is not None:
            raise Removed(REMOVED)
    members = {int(row.id): row for row in _members(session)}
    found = members.get(ticket_id)
    if found is None:
        raise NotQueued(ticket_id)
    return found


def _prompt_alerts(prompt: str) -> List[Dict[str, Any]]:
    """The run's opening text, which is the allow-listed alert fields."""
    alerts = []
    for block in prompt.split("\n\n"):
        fields: Dict[str, str] = {}
        current = ""
        names = {"time", "title", "rule", "severity", "entity keys", "description"}
        for line in block.splitlines():
            name, sep, value = line.partition(":")
            key = name.strip().lower()
            if sep and key in names:
                current = key
                fields[current] = value.strip()
            elif current:
                fields[current] = f"{fields[current]}\n{line}".strip()
        if "title" not in fields and "description" not in fields:
            continue
        keys = [key for key in (fields.get("entity keys") or "").split(", ") if key]
        alerts.append(
            _content(
                time=fields.get("time") or "",
                title=fields.get("title") or "",
                rule=fields.get("rule") or "",
                severity=fields.get("severity") or "",
                entity_keys=keys,
                description=fields.get("description") or "",
            )
        )
    return alerts


def _run_prompt(session: Session, run_id: str) -> str:
    payload = session.execute(
        text("""
            SELECT payload FROM agent_events
            WHERE kind = 'run' AND run_id::text = :run_id
            ORDER BY seq LIMIT 1
            """),
        {"run_id": run_id},
    ).scalar()
    if not isinstance(payload, dict):
        return ""
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        return ""
    prompt = spec.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _alerts(session: Session, row: _Ticket) -> List[Dict[str, Any]]:
    wanted = [ref for ref in row.alert_ids if ref]
    chosen = []
    if wanted:
        chosen = list(
            session.scalars(
                select(HistoryAlert)
                .where(
                    HistoryAlert.import_id == row.import_id,
                    HistoryAlert.alert_id.in_(wanted),
                )
                .order_by(HistoryAlert.alert_id)
            )
        )
    if chosen:
        return [
            _content(
                time=alert.alert_time,
                title=alert.title,
                rule=alert.rule_id,
                severity=alert.severity,
                entity_keys=list(alert.entity_keys or []),
                description=alert.description,
            )
            for alert in chosen
        ]
    # A connected pull keeps no alert row. The run's prompt is those fields.
    prompted = _prompt_alerts(_run_prompt(session, row.run_id))
    if prompted:
        return prompted
    when = row.opened_at or row.closed_at
    return [
        _content(
            time=when,
            title=row.title,
            rule=row.rule_id,
            severity=row.severity,
            entity_keys=list(row.entity_keys or []),
            description="",
        )
    ]


def _actions(raw: Any) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return actions
    for item in raw:
        if not isinstance(item, dict):
            continue
        confidence = item.get("confidence")
        actions.append(
            {
                "action_type": str(item.get("action_type") or ""),
                "target": str(item.get("target") or ""),
                "reason": str(item.get("reason") or ""),
                "confidence": (
                    confidence if isinstance(confidence, (int, float)) else None
                ),
            }
        )
    return actions


def _evidence(session: Session, run_id: str) -> List[Dict[str, Any]]:
    rows = []
    for payload in session.execute(_TOOL_CALLS, {"run_id": run_id}).scalars():
        if not isinstance(payload, dict):
            continue
        tool = str(payload.get("tool") or "")
        # conclude is the answer, and a refused one quotes the write-up.
        if tool == "conclude":
            continue
        result = payload.get("result")
        rows.append(
            {
                "tool": tool,
                "result": result if isinstance(result, str) else "",
            }
        )
    return rows


def ticket_view(
    session: Session,
    ticket_id: int,
    *,
    actor: Optional[str],
    see_text: bool,
    see_ledger: bool,
) -> Dict[str, Any]:
    """Alert text and ledger evidence, and neither verdict until a call is stored."""
    row = _row(session, ticket_id)
    reviews = _review_rows(session, [ticket_id]).get(ticket_id, [])
    case_id = _case_by_run(session, [row.run_id]).get(row.run_id)
    body = _summary(row, reviews, actor, case_id)
    body["removed"] = False
    body["notice"] = None
    body["alerts"] = _alerts(session, row) if see_text else None
    body["alerts_hidden"] = not see_text
    body["evidence"] = _evidence(session, row.run_id) if see_ledger else None
    body["evidence_hidden"] = not see_ledger
    if reviews:
        body["closure_verdict"] = row.closure
        body["vigil_verdict"] = row.vigil["verdict"]
        body["summary"] = row.vigil["summary"]
        body["proposed_actions"] = _actions(row.vigil["proposed_actions"])
        body["run_id"] = row.run_id
    else:
        body["closure_verdict"] = None
        body["vigil_verdict"] = None
        body["summary"] = None
        body["proposed_actions"] = None
        body["run_id"] = None
    if not see_text:
        body["title"] = None
    return body


def confirm(
    session: Session,
    ticket_id: int,
    call: str,
    *,
    actor: str,
    see_text: bool,
    see_ledger: bool,
) -> Dict[str, Any]:
    """Append the call. The first row stays the one that counts."""
    if call not in CALLS:
        raise ValueError(call)
    row = _row(session, ticket_id)
    session.add(RecheckReview(ticket_id=ticket_id, call=call, actor=actor))
    session.flush()
    trail_events.record(
        session,
        REVIEWED,
        actor=actor,
        ticket_id=str(ticket_id),
        batch_id=row.batch_id,
        call=call,
    )
    return ticket_view(
        session, ticket_id, actor=actor, see_text=see_text, see_ledger=see_ledger
    )


def _case_body(row: Any, alerts: List[Dict[str, Any]]) -> str:
    lines = [
        "Opened from a re-check of a ticket closed on "
        f"{_iso(row.closed_at)} in {row.source}.",
        f"Run: {row.run_id}",
        "",
    ]
    for alert in alerts:
        keys = ", ".join(str(key) for key in (alert.get("entity_keys") or []))
        lines.extend(
            (
                f"time: {alert.get('time') or ''}",
                f"title: {alert.get('title') or ''}",
                f"rule: {alert.get('rule') or ''}",
                f"severity: {alert.get('severity') or ''}",
                f"entity keys: {keys}",
                f"description: {alert.get('description') or ''}",
                "",
            )
        )
    return "\n".join(lines).strip()


def open_reviewed_case(
    session: Session, ticket_id: int, *, actor: str
) -> Dict[str, str]:
    """Open a Case from the first call, with no finding and no intake."""
    row = _row(session, ticket_id)
    reviews = _review_rows(session, [ticket_id]).get(ticket_id, [])
    existing = _case_by_run(session, [row.run_id]).get(row.run_id)
    if existing:
        return {"case_id": existing}
    if not _may_open(row, reviews, actor, None):
        raise CannotOpen(ticket_id)
    alerts = _alerts(session, row)
    title = case_title(str(alerts[0].get("title") or "") or f"Re-check {row.ticket_id}")
    severity = str(alerts[0].get("severity") or "")
    priority = (
        severity if severity in ("critical", "high", "medium", "low") else "medium"
    )
    mark = {
        "source": row.source,
        "closed_at": _iso(row.closed_at),
        "run_id": row.run_id,
    }
    opened = open_case(
        DatabaseDataService(),
        title=title or f"Re-check {row.ticket_id}",
        finding_ids=[],
        priority=priority,
        description=_case_body(row, alerts),
        status="open",
        recheck_mark=mark,
    )
    if not opened:
        again = _case_by_run(session, [row.run_id]).get(row.run_id)
        if again:
            return {"case_id": again}
        raise CannotOpen(ticket_id)
    return {"case_id": opened["case_id"]}


def case_recheck(session: Session, case_id: str) -> Dict[str, Any]:
    """The mark, and whether a later call differs. Names nobody."""
    mark = session.execute(
        select(Case.recheck_mark).where(Case.case_id == case_id)
    ).scalar_one_or_none()
    if not isinstance(mark, dict) or not mark.get("run_id"):
        return {"recheck": None, "reviewers_differ": False}
    run_id = str(mark["run_id"])
    calls = list(
        session.execute(
            text("""
                SELECT r.call
                FROM recheck_reviews r
                JOIN capped_recheck_tickets t ON t.id = r.ticket_id
                WHERE t.run_id = :run_id
                ORDER BY r.id
                """),
            {"run_id": run_id},
        ).scalars()
    )
    return {
        "recheck": {
            "source": str(mark.get("source") or ""),
            "closed_at": str(mark.get("closed_at") or ""),
            "run_id": run_id,
        },
        "reviewers_differ": bool(calls) and any(call != calls[0] for call in calls[1:]),
    }
