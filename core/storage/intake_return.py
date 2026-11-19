"""Send an alert back to intake when it is taken off the Case it was merged into.

Runs in the removal's transaction. A committed removal of such an alert carries
the new queued row or a note that it could not be queued; a removal that rolls
back leaves neither (#4850).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from core.storage.activity_actor_hook import fields as actor_fields
from core.storage.intake_queue import insert_intake_trigger, intake_severity_band
from core.storage.models import Case, Finding, IntakeTrigger
from core.time import utcnow

logger = logging.getLogger(__name__)


def return_removed_findings(
    session: Session, case: Case, finding_ids: Iterable[str]
) -> None:
    """Queue alerts this removal undoes. Never raises: the removal stands."""
    ids = [f for f in finding_ids if f]
    if not ids:
        return
    actor = _actor(session)
    for finding_id in ids:
        _one(session, case, finding_id, actor)


def _actor(session: Session) -> dict:
    try:
        with session.begin_nested():
            return actor_fields(session)
    except Exception:
        logger.exception("could not name who sent an alert back to intake")
        return {"actor_text": "agent"}


def _one(session: Session, case: Case, finding_id: str, actor: dict) -> None:
    try:
        with session.begin_nested():
            if not _eligible(session, case, finding_id):
                return
            title = _title(session, finding_id)
            severity = _severity(session, finding_id)
            new_id = insert_intake_trigger(
                kind="detection",
                finding_id=finding_id,
                priority=intake_severity_band("detection", finding_severity=severity),
                session=session,
            )
            # Already queued: nothing more, and not a failure.
            if new_id is None:
                return
            _note(
                session,
                case,
                "sent_back_to_intake",
                f"Alert `{title}` was sent back to intake",
                finding_id,
                actor,
            )
    except Exception:
        logger.exception(
            "could not send finding %s back to intake from case %s",
            finding_id,
            case.case_id,
        )
        _note_failure(session, case, finding_id, actor)


def _note_failure(session: Session, case: Case, finding_id: str, actor: dict) -> None:
    try:
        with session.begin_nested():
            _note(
                session,
                case,
                "intake_failure",
                f"Alert `{_title(session, finding_id)}` couldn't be sent back to "
                "intake and can be sent to intake by hand",
                finding_id,
                actor,
            )
    except Exception:
        logger.exception(
            "could not record on case %s that finding %s was not sent back to intake",
            case.case_id,
            finding_id,
        )


def _eligible(session: Session, case: Case, finding_id: str) -> bool:
    """Latest decision is a merge into this Case (or one later merged into it),
    nothing for this alert ever launched, and it is not marked noise."""
    finding = session.get(Finding, finding_id)
    if finding is None or finding.noise_marked_at is not None:
        return False
    launched = (
        session.query(IntakeTrigger.id)
        .filter(
            IntakeTrigger.finding_id == finding_id,
            IntakeTrigger.state == "launched",
        )
        .first()
    )
    if launched is not None:
        return False
    latest = (
        session.query(IntakeTrigger)
        .filter(IntakeTrigger.finding_id == finding_id)
        .order_by(IntakeTrigger.id.desc())
        .first()
    )
    if latest is None or latest.state != "merged" or not latest.merged_into:
        return False
    if latest.merged_into == case.case_id:
        return True
    return latest.merged_into in _cases_merged_into(case)


def _cases_merged_into(case: Case) -> set[str]:
    """Cases ``merge_cases`` folded into this one, including through later merges.

    A hand-made relationship does not count: only the ``case_merged`` activity
    the merge writes, which it also copies onto the target.
    """
    incoming: dict[str, set[str]] = {}
    for activity in case.activities or []:
        if (
            not isinstance(activity, dict)
            or activity.get("activity_type") != "case_merged"
        ):
            continue
        details = activity.get("details") or {}
        source = details.get("source_case_id")
        target = details.get("target_case_id")
        if isinstance(source, str) and source and isinstance(target, str) and target:
            incoming.setdefault(target, set()).add(source)
    case_id = case.case_id
    found: set[str] = set()
    stack = [case_id]
    while stack:
        current = stack.pop()
        for source in incoming.get(current, ()):
            if source not in found and source != case_id:
                found.add(source)
                stack.append(source)
    return found


def _title(session: Session, finding_id: str) -> str:
    finding = session.get(Finding, finding_id)
    title = finding.title if finding is not None else None
    return title or finding_id


def _severity(session: Session, finding_id: str) -> Optional[str]:
    finding = session.get(Finding, finding_id)
    return finding.severity if finding is not None else None


def _note(
    session: Session,
    case: Case,
    activity_type: str,
    description: str,
    finding_id: str,
    actor: dict,
) -> None:
    case.activities = [
        *(case.activities or []),
        {
            "timestamp": utcnow().isoformat() + "Z",
            "activity_type": activity_type,
            "description": description,
            "details": {"finding_id": finding_id},
            **actor,
        },
    ]
    session.flush()
