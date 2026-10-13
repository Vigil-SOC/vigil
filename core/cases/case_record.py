"""One newest-first read of a case's record.

The run's ledger comes from the agent. ``case_audit_logs`` is read here, by
``entity_type`` and ``entity_id``: the table has no ``case_id`` column. The
case's own history is ``cases.activities``, appended to by runs, analysts and
merges; it is read off the case, not written here. This view does not query
``agent_events``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.storage.models import CaseAuditLog


def _at(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse(value: Any) -> Optional[datetime]:
    """A timestamp as an aware datetime; None when it is not one.

    Writers disagree: runs send ``...123Z``, activities naive UTC plus ``Z``,
    ``_at`` ``+00:00``. As strings they do not sort together.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _short(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= 280 else text[:277] + "..."


def _join(*parts: Any) -> str:
    kept = [
        str(part).strip() for part in parts if part is not None and str(part).strip()
    ]
    return " — ".join(kept)


def event_text(kind: str, payload: Any) -> str:
    body = payload if isinstance(payload, Mapping) else {}
    if kind == "decision":
        return _join(body.get("action"), body.get("rationale")) or kind
    if kind == "finding":
        return _join(body.get("agent_id"), _short(body.get("answer"))) or kind
    if kind == "dispatch":
        return (
            _join(
                body.get("query_intent") or body.get("agent_id"),
                body.get("failure_reason"),
            )
            or kind
        )
    if kind == "terminal":
        return _join(body.get("outcome"), body.get("reason")) or kind
    if kind == "resolution":
        return _join(body.get("answer"), body.get("text")) or kind
    if kind == "directive":
        return _join(body.get("kind"), body.get("text")) or kind
    if kind == "recall":
        return "Opening recall"
    return kind


def audit_text(row: CaseAuditLog) -> str:
    summary = (row.change_summary or "").strip()
    if summary:
        return summary
    change = f"{row.action} {row.field_name or ''}".strip()
    if row.old_value or row.new_value:
        return f"{change}: {row.old_value or '—'} → {row.new_value or '—'}"
    return change or row.action


def merge_record(
    events: Sequence[Mapping[str, Any]],
    audits: Sequence[CaseAuditLog],
    activities: Sequence[Any] = (),
) -> list[dict]:
    """Run rows, case-audit rows and case-activity rows, newest first.

    Run rows are the chained ledger. Audit and activity rows are not: the page
    says so.
    """
    rows: list[dict] = []
    for event in events:
        kind = str(event.get("kind") or "")
        seq = event.get("seq")
        rows.append(
            {
                "id": f"run:{seq}",
                "at": str(event.get("ts") or ""),
                "kind": kind,
                "source": "run",
                "chained": True,
                "text": event_text(kind, event.get("payload")),
                # A turn lands in one transaction, so every row shares `now()`.
                # Seq is the order inside that instant.
                "_seq": (
                    seq
                    if isinstance(seq, (int, float)) and not isinstance(seq, bool)
                    else 0
                ),
            }
        )
    for audit in audits:
        rows.append(
            {
                "id": f"audit:{audit.audit_id}",
                "at": _at(audit.timestamp),
                "kind": "case_audit_logs",
                "source": "case_audit_logs",
                "chained": False,
                "text": audit_text(audit),
                "_seq": 0,
            }
        )
    # Activities have no id; the list is only appended to, so the index is stable.
    for index, activity in enumerate(activities):
        if not isinstance(activity, Mapping):
            continue
        at = _parse(activity.get("timestamp"))
        if at is None:
            continue
        kind = str(activity.get("activity_type") or "")
        rows.append(
            {
                "id": f"activity:{index}",
                "at": _at(at),
                "kind": kind,
                "source": "case_activities",
                "chained": False,
                "text": _short(activity.get("description")) or kind,
                "_seq": 0,
            }
        )
    rows.sort(key=lambda row: (_parse(row["at"]) or _NEVER, row["_seq"]), reverse=True)
    for row in rows:
        row.pop("_seq", None)
    return rows
