"""Customer-specific exceptions to one overlay copy, kept apart from its body (#3794).

An exception is ``{field: value}`` in the copy's own field names (ECS for the
Elastic copy, Splunk's for the Splunk copy, Sigma's for the Sigma form). A
document matches when every pair equals its field (a dotted path, flat key
first); a list field matches when any element does, and a CIDR value matches an
address inside it. Vigil's runner drops matching documents before they are
counted or become findings; save_detection_rule's replay counts a ruled-out
event a Sigma exception matches as excluded. Adding or removing one makes no
rule version. Removal fills in the removal columns; nothing is deleted.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import select

from core.detections.inventory import readers
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleException

ACTIONS = ("add", "remove")
_ABSENT = object()


def _int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def checked(match: Any, note: Any) -> Tuple[Dict[str, Any], str]:
    """(match, note) as stored; ValueError naming what is wrong."""
    if not isinstance(match, dict) or not match:
        raise ValueError("match must be a non-empty {field: value} object")
    for field, value in match.items():
        if not isinstance(field, str) or not field.strip():
            raise ValueError("every match field needs a name")
        if value is None or isinstance(value, (dict, list)):
            raise ValueError(f"match value for {field!r} must be a single value")
    note = str(note or "").strip()
    if not note:
        raise ValueError("a note saying why this is expected is required")
    return {k.strip(): v for k, v in match.items()}, note


def _resolve(doc: Any, field: str) -> Any:
    """Flat key first, else walk maps; a list of maps (an EQL sequence) yields each one's value."""
    if isinstance(doc, list):
        found = [v for d in doc if (v := _resolve(d, field)) is not _ABSENT]
        return found or _ABSENT
    if not isinstance(doc, dict):
        return _ABSENT
    if field in doc:
        return doc[field]
    head, _, rest = field.partition(".")
    if not rest or head not in doc:
        return _ABSENT
    return _resolve(doc[head], rest)


def _network(value: Any) -> Optional[Any]:
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return None


def _equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return any(_equals(a, expected) for a in actual)
    if actual is None or isinstance(actual, dict):
        return False
    network = _network(expected)
    if network is not None:
        try:
            return ipaddress.ip_address(str(actual).strip()) in network
        except ValueError:
            return str(actual) == expected
    return _text(actual) == _text(expected)


def _text(value: Any) -> str:
    # Splunk rows are all strings: true == "true", 443 == "443" == 443.0.
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def matches(match: Mapping[str, Any], doc: Mapping[str, Any]) -> bool:
    """Every pair of ``match`` equals ``doc``'s field."""
    if not isinstance(doc, dict):
        return False
    for field, expected in match.items():
        actual = _resolve(doc, field)
        if actual is _ABSENT or not _equals(actual, expected):
            return False
    return True


def split(
    docs: List[Dict[str, Any]], excepted: List[Tuple[int, Dict[str, Any]]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """(docs no exception matches, matches per exception id) over hit ``_source``s.

    A document two exceptions match counts once, against the first.
    """
    counts = {str(eid): 0 for eid, _ in excepted}
    kept = []
    for doc in docs:
        source = doc.get("_source") if isinstance(doc, dict) else None
        hit = next(
            (eid for eid, m in excepted if matches(m, source or {})),
            None,
        )
        if hit is None:
            kept.append(doc)
        else:
            counts[str(hit)] += 1
    return kept, counts


def active(rule_ids: List[int]) -> Dict[int, List[Tuple[int, Dict[str, Any]]]]:
    """Active exceptions by rule id, oldest first: (id, match)."""
    out: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
    if not rule_ids:
        return out
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRuleException.rule_id,
                DetectionRuleException.id,
                DetectionRuleException.match,
            )
            .where(
                DetectionRuleException.rule_id.in_(list(rule_ids)),
                DetectionRuleException.removed_at.is_(None),
            )
            .order_by(DetectionRuleException.id)
        ).all()
    for rid, eid, match in rows:
        out.setdefault(rid, []).append((eid, match or {}))
    return out


def listed(session: Any, rule_id: int) -> List[Dict[str, Any]]:
    """A rule's active exceptions: id, match, note, created_by."""
    rows = session.scalars(
        select(DetectionRuleException)
        .where(
            DetectionRuleException.rule_id == rule_id,
            DetectionRuleException.removed_at.is_(None),
        )
        .order_by(DetectionRuleException.id)
    ).all()
    return [_row(r) for r in rows]


def _row(r: DetectionRuleException) -> Dict[str, Any]:
    return {
        "exception_id": r.id,
        "rule_id": r.rule_id,
        "match": r.match,
        "note": r.note,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "removed_at": r.removed_at.isoformat() if r.removed_at else None,
        "removed_by": r.removed_by,
        "removal_reason": r.removal_reason,
    }


def add_many(
    session: Any, rule_id: int, items: List[Tuple[Dict[str, Any], str]], who: str
) -> List[Dict[str, Any]]:
    """Add each (match, note) to ``rule_id``; one already active with that match stays."""
    have = {
        repr(sorted(m.items()))
        for m in session.scalars(
            select(DetectionRuleException.match).where(
                DetectionRuleException.rule_id == rule_id,
                DetectionRuleException.removed_at.is_(None),
            )
        ).all()
    }
    added = []
    for match, note in items:
        key = repr(sorted(match.items()))
        if key in have:
            continue
        have.add(key)
        row = DetectionRuleException(
            rule_id=rule_id, match=match, note=note, created_by=who
        )
        session.add(row)
        session.flush()
        added.append(_row(row))
    return added


def _who() -> str:
    from core.detections.inventory.links import _who as who

    return who()[0]


def edit_exception(
    action: str,
    rule_id: Any = None,
    match: Any = None,
    note: Any = None,
    exception_id: Any = None,
    reason: Any = None,
) -> Dict[str, Any]:
    """Add an exception to an overlay copy, or remove one; attributed to the caller."""
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        return {"error": f"action must be one of {', '.join(ACTIONS)}"}
    who = _who()
    with get_db_manager().session_scope() as session:
        if action == "add":
            rid = _int(rule_id)
            if rid is None:
                return {"error": "rule_id must be an overlay copy's rule_id"}
            try:
                match, note = checked(match, note)
            except ValueError as exc:
                return {"error": str(exc)}
            rule = session.get(DetectionRule, rid)
            if rule is None:
                return {"error": f"No detection rule {rid}"}
            if rule.source != readers.OVERLAY_SOURCE:
                return {"error": f"Rule {rid} is not one of Vigil's overlay copies"}
            if rule.status == "removed":
                return {"error": f"Rule {rid} is removed"}
            added = add_many(session, rid, [(match, note)], who)
            if not added:
                return {
                    "error": f"Rule {rid} already has an active exception with that match"
                }
            return {"success": True, "action": "add", "exception": added[0]}
        eid = _int(exception_id)
        reason = str(reason or "").strip()
        if eid is None:
            return {"error": "exception_id is required to remove"}
        if not reason:
            return {"error": "a reason is required to remove an exception"}
        row = session.get(DetectionRuleException, eid, with_for_update=True)
        if row is None:
            return {"error": f"No exception {eid}"}
        if row.removed_at is not None:
            return {
                "error": f"Exception {eid} is already removed",
                "exception": _row(row),
            }
        row.removed_at = datetime.now(timezone.utc)
        row.removed_by, row.removal_reason = who, reason
        session.flush()
        return {"success": True, "action": "remove", "exception": _row(row)}
