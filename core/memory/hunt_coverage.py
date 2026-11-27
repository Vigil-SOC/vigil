"""Is a threat report already covered by a hunt? (#903, #5294)

``parse_report`` turns the report into Entity Keys and T-IDs; each is answered
from its ``episodic_coverage_entries`` under the one rule below, which lives
here and nowhere else. Status, in-flight first (epic #886, decision 2):

- ``running``   -- a hunt is on one of them now; extend that run rather than
  start another (the in-flight half of ``list_prior_hunts``).
- ``uncovered`` -- at least one is due; the proposal is a body
  ``POST /api/workflows/threat-hunt/execute`` accepts as-is. ``with_claim``
  swaps its template hypothesis for the report's own claim (#1918).
- ``concluded`` -- every one is covered by a recent entry; a proposal is still
  offered should the operator want a fresh look.

The rule: the newest entry decides. proven, disproven and handed_off cover for
the key type's window from ``concluded_at``; inconclusive covers for ``RETRY``
doubled per consecutive inconclusive entry since the last other outcome, capped
at the window. An analyst's proven entry after an agent's disproven one, a
reopen entry, or no entry at all is due now. An asked technique due by its own
entries is still covered while a sub-technique under it is (``covered_by``).
``reopen_for`` says when a covered key's changed feed object earns a reopen.

Entries are read as the caller: hunt entries through ``_hidden_hunts``, Case
entries through ``case_read_clause``; reopen entries name no investigation and
are visible to all. Nothing here starts a hunt or files a directive. Both the
``check_hunt_coverage`` backend tool and the ``/workflows/threat-hunt/coverage``
route call this.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from core.memory.entity_keys import normalise_keys
from core.memory.hunt_claim import claim_from
from core.memory.prior_hunts import (
    _hidden_hunts,
    _iso,
    expand_techniques,
    in_flight_hunts,
    normalise_techniques,
)
from core.storage.models import Case
from core.storage.models.episodic import EpisodicCoverageEntry
from core.storage.unit_of_work import unit_of_work
from core.threat_intel.threat_feed_service import parse_report

EXECUTE_PATH = "/api/workflows/threat-hunt/execute"
EXTEND_PATH = "/api/agent-runs/{run_id}/directives"

# Defaults, not settings (epic #5286, D2/D3): how long a clean answer stands per
# Entity Key type, and the first retry after an inconclusive one. RETRY is also
# the feed poller's hold on a launched intel row's keys.
WINDOWS = {
    "ip": timedelta(days=14),
    "url": timedelta(days=14),
    "domain": timedelta(days=30),
    "hash": timedelta(days=180),
}
TECHNIQUE_WINDOW = timedelta(days=30)
DEFAULT_WINDOW = timedelta(days=30)
RETRY = timedelta(days=3)

# Entries returned per key; the rule reads them all.
ENTRIES_SHOWN = 20

_TERMINAL = {"proven", "disproven", "handed_off"}
_RANK = {"proven": 0, "disproven": 1, "handed_off": 2, "inconclusive": 3}


def window_for(key: str) -> timedelta:
    """A T-ID's window, else the window of the key's ``type:`` prefix."""
    if normalise_techniques([key]) == [key]:
        return TECHNIQUE_WINDOW
    return WINDOWS.get(key.split(":", 1)[0], DEFAULT_WINDOW)


def _entries(session: Session, asked: Sequence[str]) -> Dict[str, List[Any]]:
    """Every visible entry for the asked keys and T-IDs, newest first, in one read."""
    from core.auth.permissions import case_read_clause  # deferred, as prior_hunts does

    e = EpisodicCoverageEntry
    hidden = _hidden_hunts(session)
    hunt = and_(e.investigation_kind == "hunt", e.investigation_id.notin_(hidden))
    statement = (
        select(e)
        .outerjoin(
            Case,
            and_(e.investigation_kind == "case", Case.case_id == e.investigation_id),
        )
        .where(
            e.key.in_(asked),
            or_(
                e.method == "reopen",
                hunt if hidden else e.investigation_kind == "hunt",
                and_(e.investigation_kind == "case", case_read_clause()),
                e.investigation_kind == "analyst",
            ),
        )
        .order_by(e.concluded_at.desc(), e.id.desc())
    )
    by_key: Dict[str, List[Any]] = {key: [] for key in asked}
    for entry in session.scalars(statement):
        by_key[entry.key].append(entry)
    return by_key


def _checks(entries: Sequence[Any]) -> List[Any]:
    """One entry per check, newest first. A hunt writes an entry per hypothesis
    naming the key, all at one ``concluded_at``; it is one check, and its
    strongest outcome speaks for it (proven, disproven, handed off, inconclusive)."""
    checks: Dict[tuple, Any] = {}
    for entry in entries:
        at = (entry.method, entry.investigation_kind, entry.investigation_id)
        at += (entry.concluded_at,)
        held = checks.get(at)
        if held is None or _RANK.get(entry.outcome, 9) < _RANK.get(held.outcome, 9):
            checks[at] = entry
    return list(checks.values())  # dicts keep first-seen order: newest first


def due_at(key: str, entries: Sequence[Any]) -> Optional[datetime]:
    """When ``key`` falls due from its entries (newest first); None is due now."""
    checks = _checks(entries)
    if not checks or checks[0].method == "reopen":
        return None
    newest = checks[0]
    window = window_for(key)
    if newest.outcome in _TERMINAL:
        earlier = next((x for x in checks[1:] if x.method != "reopen"), None)
        reversed_ = (
            newest.outcome == "proven"
            and newest.trust == "analyst"
            and earlier is not None
            and earlier.outcome == "disproven"
            and earlier.trust == "agent"
        )
        until = None if reversed_ else newest.concluded_at + window
    else:
        # inconclusive: count the run back to the last terminal check
        run = 0
        for check in checks:
            if check.outcome in _TERMINAL:
                break
            if check.outcome == "inconclusive":
                run += 1
        # capped before multiplying: 2**n days overflows timedelta past n≈28
        until = newest.concluded_at + min(RETRY * 2 ** min(run - 1, 16), window)
    return until if until and until > datetime.now(timezone.utc) else None


def _at(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from a datetime or ISO text; None when unreadable."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reopen_for(
    key: str, entries: Sequence[Dict[str, Any]], changed_at: Any
) -> Optional[Dict[str, Any]]:
    """The check a covered key's feed object postdates, when it should reopen (D5).

    ``entries`` are the key's answered entries (newest first). The key reopens
    when the object changed after the newest check that covers it, and no
    reopen entry falls inside the key's window. Else None.
    """
    changed = _at(changed_at)
    covering = next((x for x in entries if x["method"] != "reopen"), None)
    if changed is None or covering is None:
        return None
    concluded = _at(covering["concluded_at"])
    if concluded is None or changed <= concluded:
        return None
    since = datetime.now(timezone.utc) - window_for(key)
    for x in entries:
        at = _at(x["concluded_at"])
        if x["method"] == "reopen" and at is not None and at >= since:
            return None
    return covering


def _answer(key: str, entries: Sequence[Any]) -> Dict[str, Any]:
    return {
        "key": key,
        "due_at": _iso(due_at(key, entries)),
        "entries": [
            {
                "outcome": x.outcome,
                "method": x.method,
                "trust": x.trust,
                "concluded_by": x.concluded_by,
                "concluded_at": _iso(x.concluded_at),
                "investigation_kind": x.investigation_kind,
                "investigation_id": x.investigation_id,
                "run_id": str(x.run_id) if x.run_id else None,
                "reason": x.reason,
            }
            for x in entries[:ENTRIES_SHOWN]
        ],
        "entries_total": len(entries),
    }


def build_proposal(
    keys: Sequence[str], techniques: Sequence[str], claim: Optional[str] = None
) -> Dict[str, Any]:
    """A ``WorkflowExecuteRequest`` body. The hypothesis is the report's
    ``claim`` when it has one; else an indicator template, whose T-IDs go in the
    text because that is the only place the request carries them, and where the
    in-flight matcher reads them from once the hunt is running."""
    parts = []
    if keys:
        parts.append(f"indicators {', '.join(keys)}")
    if techniques:
        parts.append(f"techniques {', '.join(techniques)}")
    # One line: execute_workflow reads each hypothesis as a line of text, so a
    # key carrying a newline would split the statement from its subjects.
    hypothesis = claim or " ".join(
        f"Activity from the reported {' and '.join(parts)} is present in the "
        "environment".split()
    )
    return {
        "hypothesis": hypothesis,
        "hypothesis_subjects": {hypothesis: list(keys)},
        "approve_hypotheses": True,
    }


def check_coverage(
    report: Optional[str] = None,
    entity_keys: Iterable[str] = (),
    techniques: Iterable[str] = (),
    *,
    session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Classify a report (and/or already-parsed keys and T-IDs) as
    ``running``, ``concluded`` or ``uncovered``, with each key's entries.

    Raises ``ValueError`` when there is nothing to ask about: an empty report
    with no keys is a caller mistake, not a coverage answer.
    """
    parsed = parse_report(report) if report else {"entity_keys": [], "techniques": []}
    keys = normalise_keys([*parsed["entity_keys"], *entity_keys])
    t_ids = normalise_techniques([*parsed["techniques"], *techniques])
    if not keys and not t_ids:
        raise ValueError("nothing to check: no entity keys or techniques were found")

    # An asked technique is also covered by a covered sub-technique under it
    # (#5292): T1078 by T1078.004, AML.T0051 by AML.T0051.000.
    under = {t: [x for x in expand_techniques([t]) if x != t] for t in t_ids}
    asked = [*keys, *t_ids, *{x for subs in under.values() for x in subs}]
    with unit_of_work(session) as db:
        found = _entries(db, list(dict.fromkeys(asked)))
        coverage = [_answer(key, found[key]) for key in [*keys, *t_ids]]
    for row in coverage:
        if row["due_at"] or not under.get(row["key"]):
            continue
        via = {sub: due_at(sub, found[sub]) for sub in under[row["key"]]}
        via = {sub: at for sub, at in via.items() if at}
        if via:
            row["due_at"] = _iso(max(via.values()))
            row["covered_by"] = sorted(via)
    covered = {row["key"] for row in coverage if row["due_at"]}
    in_flight = in_flight_hunts(keys, t_ids)

    if in_flight:
        status = "running"
    elif len(covered) < len(coverage):
        status = "uncovered"
    else:
        status = "concluded"

    result: Dict[str, Any] = {
        "status": status,
        "keys": keys,
        "techniques": t_ids,
        "matched_keys": [k for k in keys if k in covered],
        "unmatched_keys": [k for k in keys if k not in covered],
        "matched_techniques": [t for t in t_ids if t in covered],
        "unmatched_techniques": [t for t in t_ids if t not in covered],
        "coverage": coverage,
    }
    if status == "running":
        result["in_flight"] = in_flight
        result["extend"] = {
            "method": "POST",
            "path": EXTEND_PATH,
            "kind": "extend",
            "run_ids": [row["run_id"] for row in in_flight],
        }
    else:
        result["proposal"] = build_proposal(keys, t_ids)
        result["execute"] = {"method": "POST", "path": EXECUTE_PATH}
    return result


async def with_claim(result: Dict[str, Any], report: str) -> Dict[str, Any]:
    """``result`` with the report's own claim as the proposed hypothesis (#1918).

    The indicators stay the hypothesis's subjects. A report already being hunted
    gets a proposal too, beside its ``running`` status, so a repeat paste still
    has something to accept. With no claim the template stays.
    """
    claim = await claim_from(report)
    if claim or result["status"] == "running":
        result["proposal"] = build_proposal(result["keys"], result["techniques"], claim)
        result["execute"] = {"method": "POST", "path": EXECUTE_PATH}
    return result
