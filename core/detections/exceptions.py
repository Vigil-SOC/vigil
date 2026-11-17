"""Customer-specific exceptions to one overlay copy, kept apart from its body (#3794).

An exception is ``{field: value}`` in the copy's own field names (ECS for the
Elastic copy, Splunk's for the Splunk copy, Sigma's for the Sigma form). A
document matches when every pair equals its field (a dotted path, flat key
first); a list field matches when any element does, and a CIDR value matches an
address inside it. Vigil's runner drops matching documents before they are
counted or become findings; save_detection_rule's replay counts a ruled-out
event a Sigma exception matches as excluded. Adding or removing one makes no
rule version. Removal fills in the removal columns; nothing is deleted. Each
add and removal is written to the config audit log in the same transaction.

An exception an agent adds to a copy that already has alerts judged false
cites one or more of those alerts that the exception matches. A person's
exception, a Sigma copy, and a handed-off copy store no citations.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from sqlalchemy import select

from core.detections.inventory import readers
from core.storage.config_service import ConfigService
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleException,
    DetectionRuleVersion,
)

ACTIONS = ("add", "remove")
AUDIT_TYPE = "detection_rule_exception"
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
        network = _network(value)
        if network is not None and network.prefixlen == 0:
            raise ValueError(
                f"{field} = {value} is a /0 network: it would match every event "
                f"that has {field}, so the rule could never raise anything"
            )
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


def absent(
    docs: List[Dict[str, Any]], excepted: List[Tuple[int, Dict[str, Any]]]
) -> Dict[str, List[str]]:
    """Per exception id, its match fields no hit's ``_source`` carries (#3907),
    e.g. an input field of a transforming search, whose rows are its output."""
    sources = [(d.get("_source") if isinstance(d, dict) else None) or {} for d in docs]
    return {
        str(eid): [f for f in m if all(_resolve(s, f) is _ABSENT for s in sources)]
        for eid, m in excepted
    }


def split(
    docs: List[Dict[str, Any]], excepted: List[Tuple[int, Dict[str, Any]]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, List[str]]]:
    """(docs no exception matches, matches per exception id, each one's fields
    absent from every doc) over hit ``_source``s.

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
    return kept, counts, absent(docs, excepted)


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


def listed(session: Any, rule_id: int, removed: bool = False) -> List[Dict[str, Any]]:
    """A rule's active exceptions (and its removed ones too with ``removed``)."""
    query = select(DetectionRuleException).where(
        DetectionRuleException.rule_id == rule_id
    )
    if not removed:
        query = query.where(DetectionRuleException.removed_at.is_(None))
    rows = session.scalars(query.order_by(DetectionRuleException.id)).all()
    return [_row(r) for r in rows]


def _in_window(days: Any, since_day: str) -> int:
    """Matches counted on UTC days from ``since_day`` on."""
    if not isinstance(days, dict):
        return 0
    return sum(v for d, v in days.items() if d >= since_day and isinstance(v, int))


def of_copy(rule_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Every exception on an overlay copy, active and removed, with each one's
    per-day matches from the copy's latest version, what its last run with rows
    saw (``seen``: {rows, matched, missing, at}, or None), what the copy's
    exceptions together dropped from that run (``seen``: {rows, matched, ids,
    at}, or None), and whether a SIEM copy runs
    it instead (``handed_off``: Vigil no longer applies them).

    ``window`` puts the rule's own fires on the days the matches are counted
    for (the runner keeps KEEP_DAYS): ``fired`` the findings Fires counts for this copy (traced to it, or naming it when no other copy shares its name),
    ``silenced`` hits its current (not removed) exceptions dropped, ``hits``
    the two together. Each exception's ``silenced``, removed ones too, is its
    matches in that window.

    ``cited_finding_ids`` is what an agent cited (null when none were required).
    ``cited_false`` counts, at read time, how many of those a person currently
    judges false: a missing finding, or one later judged real, drops out.

    LookupError when there is no such rule, ValueError when it isn't an overlay copy.
    """
    from core.detections.inventory.catalog import fired_in_window
    from core.detections.inventory.rollout import KEEP_DAYS, handed_off

    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    first = today - timedelta(days=KEEP_DAYS - 1)
    since_day = first.isoformat()

    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        if rule is None:
            raise LookupError(f"No detection rule {rule_id}")
        if rule.source != readers.OVERLAY_SOURCE:
            raise ValueError(f"Rule {rule_id} is not one of Vigil's overlay copies")
        checks = session.scalar(
            select(DetectionRuleVersion.checks).where(
                DetectionRuleVersion.rule_id == rule_id,
                DetectionRuleVersion.version == rule.latest_version,
            )
        )
        counts = (checks or {}).get("exceptions") or {}
        together = (checks or {}).get("exceptions_seen")
        rows = []
        for r in listed(session, rule_id, removed=True):
            mine = counts.get(str(r["exception_id"])) or {}
            days = mine.get("days") or {}
            rows.append(
                {
                    **r,
                    "days": days,
                    "seen": mine.get("seen"),
                    "silenced": _in_window(days, since_day),
                }
            )
        _cited_false(session, rows)
    fired = fired_in_window(rule_id, since_day)
    # The current exceptions only, as the row's pill counts them; a removed
    # one keeps its own count in its row.
    silenced = sum(r["silenced"] for r in rows if not r.get("removed_at"))
    return {
        "rule_id": rule_id,
        "handed_off": rule_id in handed_off([rule_id]),
        "seen": together,
        "exceptions": rows,
        "window": {
            "days": KEEP_DAYS,
            "since": since_day,
            "fired": fired,
            "silenced": silenced,
            "hits": fired + silenced,
        },
    }


def rule_of(exception_id: int) -> Optional[int]:
    """The rule an exception belongs to, or None when there is no such exception."""
    with get_db_manager().session_scope() as session:
        return session.scalar(
            select(DetectionRuleException.rule_id).where(
                DetectionRuleException.id == exception_id
            )
        )


def _row(r: DetectionRuleException) -> Dict[str, Any]:
    return {
        "exception_id": r.id,
        "rule_id": r.rule_id,
        "match": r.match,
        "note": r.note,
        "cited_finding_ids": r.cited_finding_ids,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "removed_at": r.removed_at.isoformat() if r.removed_at else None,
        "removed_by": r.removed_by,
        "removal_reason": r.removal_reason,
    }


def _cited_false(session: Any, rows: List[Dict[str, Any]]) -> None:
    """Set ``cited_false`` on rows whose citations are a list: how many a person
    currently judges false. A missing finding, or one later judged real, drops out."""
    from core.detections.inventory import accurate
    from core.storage.models import Finding

    cited: List[str] = []
    for row in rows:
        ids = row.get("cited_finding_ids")
        if isinstance(ids, list):
            cited.extend(i for i in ids if isinstance(i, str))
    person_false: Set[str] = set()
    if cited:
        verdicts = accurate.verdicts(session, Finding.finding_id.in_(set(cited)))
        person_false = {
            fid
            for fid, verdict in verdicts.items()
            if verdict is not None and not verdict[0] and verdict[1]
        }
    for row in rows:
        ids = row.get("cited_finding_ids")
        if isinstance(ids, list):
            row["cited_false"] = sum(
                1 for i in ids if isinstance(i, str) and i in person_false
            )


def _agent_run() -> bool:
    """True when this call carries a run's signed claims, as ``overlay._author`` reads them."""
    from core.cases.agent_closure import run_agent

    agent_id, run_id = run_agent()
    return bool(agent_id or run_id)


def _judged_false(session: Any, rule_id: int) -> Set[str]:
    """Finding ids of this copy, any version, whose winning verdict says not real."""
    from core.detections.inventory import accurate
    from core.storage.models import Finding

    found = accurate.verdicts(
        session,
        Finding.entity_context["detection_rule"].contains({"rule_id": rule_id}),
    )
    return {
        fid for fid, verdict in found.items() if verdict is not None and not verdict[0]
    }


def _cite_rule(session: Any, rule_id: int) -> Optional[Set[str]]:
    """Judged-false finding ids when an agent must cite them.

    None stores null and skips the check: no run claims (a person, including a
    skill import), a Sigma copy, or a copy a SIEM now runs.
    """
    if not _agent_run():
        return None
    rule = session.get(DetectionRule, rule_id)
    if rule is None or rule.language == "sigma":
        return None
    from core.detections.inventory.rollout import handed_off

    if rule_id in handed_off([rule_id]):
        return None
    return _judged_false(session, rule_id)


def _finding_ids(rule_id: int, value: Any) -> List[str]:
    """The cited ids, in order, duplicates dropped. ValueError when none were named."""
    need = (
        f"Rule {rule_id} has alerts judged false; cite one or more finding ids "
        "of this copy that this exception matches"
    )
    if not isinstance(value, list) or not value:
        raise ValueError(need)
    ids: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("finding_ids must be finding ids")
        text = item.strip()
        if text not in ids:
            ids.append(text)
    if not ids:
        raise ValueError(need)
    return ids


def _copy_id(context: Any) -> Optional[int]:
    if not isinstance(context, dict):
        return None
    named = context.get("detection_rule")
    if not isinstance(named, dict):
        return None
    rid = named.get("rule_id")
    if isinstance(rid, bool) or not isinstance(rid, int):
        return None
    return rid


def _cut_short(evidence: Any) -> bool:
    """The stored envelope is not the whole hit (``source_evidence``)."""
    if not isinstance(evidence, dict):
        return True
    if evidence.get("truncated") or evidence.get("raw_text_truncated"):
        return True
    return evidence.get("status") != "available"


def _stored_source(evidence: Any) -> Dict[str, Any]:
    """``records[0]._source``: the document ``split`` matches. {} when it is not there."""
    if not isinstance(evidence, dict):
        return {}
    records = evidence.get("records")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return {}
    source = records[0].get("_source")
    return source if isinstance(source, dict) else {}


def _citation_problems(
    session: Any,
    rule_id: int,
    match: Mapping[str, Any],
    ids: List[str],
    false_ids: Set[str],
) -> List[str]:
    """Why each cited id cannot support this exception. A cut-short record is
    refused before it is matched, so a truncated hit is not read as a miss."""
    from core.storage.models import Finding

    rows = session.execute(
        select(Finding.finding_id, Finding.entity_context).where(
            Finding.finding_id.in_(ids)
        )
    ).all()
    found = {fid: context for fid, context in rows}
    problems = []
    for fid in ids:
        if fid not in found:
            problems.append(f"Finding {fid} was not found")
            continue
        context = found[fid]
        if _copy_id(context) != rule_id:
            problems.append(f"Finding {fid} is not an alert of this copy")
            continue
        if fid not in false_ids:
            problems.append(f"Finding {fid} was not judged false")
            continue
        evidence = context.get("source_evidence") if isinstance(context, dict) else None
        if _cut_short(evidence):
            problems.append(f"Finding {fid}'s stored record is cut short")
            continue
        if not matches(match, _stored_source(evidence)):
            problems.append(f"Finding {fid} does not match this exception")
    return problems


def _citations(
    session: Any,
    rule_id: int,
    match: Mapping[str, Any],
    raw: Any,
    false_ids: Optional[Set[str]],
) -> Optional[List[str]]:
    """Ids to store. None skips the check. [] when the copy has nothing judged false,
    and any ids passed with it are ignored."""
    if false_ids is None:
        return None
    if not false_ids:
        return []
    ids = _finding_ids(rule_id, raw)
    problems = _citation_problems(session, rule_id, match, ids, false_ids)
    if problems:
        raise ValueError("; ".join(problems))
    return ids


def _audit(
    session: Any,
    who: Tuple[str, Dict[str, Any]],
    action: str,
    old: Optional[Dict[str, Any]],
    new: Dict[str, Any],
    reason: str,
) -> None:
    from core.agents.attribution import principal_id_for_username

    caller, run = who
    # The trail names people by principal id; the exception row keeps the username.
    actor = principal_id_for_username(caller) or caller
    ConfigService(user_id=actor)._create_audit_log(
        session,
        config_type=AUDIT_TYPE,
        config_key=f"detection_rule:{new['rule_id']}",
        action=action,
        old_value=old,
        new_value={**new, **run},
        change_reason=reason,
    )


def add_many(
    session: Any,
    rule_id: int,
    items: List[Tuple[Dict[str, Any], str, Any]],
    who: Tuple[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add each (match, note, finding ids) to ``rule_id``; one already active with that match stays.

    ``who`` is ``_who()``: the caller and, for an agent's run, its agent and run id.
    An agent's citations are checked before anything is inserted. ValueError names
    the finding and why it cannot be cited; the caller's transaction rolls that back.
    """
    false_ids = _cite_rule(session, rule_id)
    # Check every item before inserting any, so a refusal leaves no row.
    prepared = [
        (match, note, _citations(session, rule_id, match, raw, false_ids))
        for match, note, raw in items
    ]
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
    for match, note, cited in prepared:
        key = repr(sorted(match.items()))
        if key in have:
            continue
        have.add(key)
        row = DetectionRuleException(
            rule_id=rule_id,
            match=match,
            note=note,
            cited_finding_ids=cited,
            created_by=who[0],
        )
        session.add(row)
        session.flush()
        added.append(_row(row))
        _audit(session, who, "create", None, added[-1], note)
    return added


def _who() -> Tuple[str, Dict[str, Any]]:
    from core.detections.inventory.links import _who as who

    return who()


def edit_exception(
    action: str,
    rule_id: Any = None,
    match: Any = None,
    note: Any = None,
    exception_id: Any = None,
    reason: Any = None,
    finding_ids: Any = None,
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
            try:
                added = add_many(session, rid, [(match, note, finding_ids)], who)
            except ValueError as exc:
                return {"error": str(exc)}
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
        old = _row(row)
        row.removed_at = datetime.now(timezone.utc)
        row.removed_by, row.removal_reason = who[0], reason
        session.flush()
        _audit(session, who, "delete", old, _row(row), reason)
        return {"success": True, "action": "remove", "exception": _row(row)}
