"""A person's undo or rejection of a containment (#5273, epic #5231 decisions 12–13).

It is written as an analyst note on the target's entity key, in the decision's
own transaction, so the next run's recall reads it. It also holds that kind on
that target for runs no person started: for ``HOLD`` after it, the autonomy
ceiling releases nothing of that kind there unless a person has approved one
since. Both are read from approval rows; nothing new is stored.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select

from core.integrations.bindings import ever_carries
from core.response.containment import CEILING_APPROVER
from core.response.proposal_targets import (
    _UPN,
    DONE_STATE,
    TARGET_RULES,
    UNDONE_STATE,
    _domain,
    _host,
    _ip,
    _is_ip,
    _user,
)
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.time import utcnow

logger = logging.getLogger(__name__)

# How long a person's reversal holds that kind on that target.
HOLD = timedelta(days=7)
# Deciders that are not a person: a run's own withdrawal, expiry, settle-on-close,
# the sign-in stop, and the ceiling.
_NOT_PEOPLE = frozenset({"agent", "system", CEILING_APPROVER})


class PersonHold(str):
    """A refusal this hold made, so ``_release`` can say it on the case."""


def by_person(name: Optional[str], principal_id: Optional[str]) -> bool:
    if not name or name in _NOT_PEOPLE or name.startswith("agent:"):
        return False
    return principal_id not in ("system",) and not (principal_id or "").startswith(
        "agent:"
    )


def _when(at: Optional[datetime]) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC") if at else "an unknown time"


def note_key(kind: str, target: Optional[str]) -> Optional[str]:
    """The target's entity key by its shape, or None (a network, an unknown kind)."""
    t = (target or "").strip()
    shape = TARGET_RULES.get(kind, (0, None))[1]
    if not t or shape is None:
        return None
    if _is_ip(t) and shape in (_host, _ip):
        kind_of_key = "ip"
    elif shape is _host:
        kind_of_key = "host"
    elif shape is _domain:
        kind_of_key = "domain"
    elif shape is _user:
        kind_of_key = "email" if _UPN.fullmatch(t) else "user"
    else:
        return None
    from core.memory.notes import NoteError, entity_key_of

    try:
        return entity_key_of(f"{kind_of_key}:{t}")
    except NoteError:
        return None


def _run_of(row: ApprovalActionRow) -> Optional[str]:
    return row.workflow_run_id or (row.parameters or {}).get("run_id")


def write_reversal_note(
    session,
    row: ApprovalActionRow,
    *,
    by: str,
    principal_id: Optional[str],
    reason: Optional[str],
    undo_verb: Optional[str] = None,
) -> None:
    """The note for a person's rejection of ``row``, or with ``undo_verb``
    (released, enabled, unblocked) their undo of it. Never raises: a savepoint
    keeps a failed insert from failing the decision."""
    from core.memory.notes import write_note

    try:
        if row.action_type not in DONE_STATE or not by_person(by, principal_id):
            return
        if not undo_verb and (row.parameters or {}).get("undoes"):
            return  # rejecting a held undo keeps the act: no reversal of it
        key = note_key(row.action_type, row.target)
        if key is None:
            return
        run = _run_of(row)
        proposed = f"proposed by run {run}" if run else "proposed by no run"
        if undo_verb:
            released = (
                "the autonomy ceiling carried it out"
                if row.approved_by == CEILING_APPROVER
                else f"{row.approved_by or 'a person'} approved it"
            )
            said = (
                f"{by} {undo_verb} {row.target} on {_when(utcnow())}, undoing "
                f"{row.action_type} {row.action_id} ({proposed}; {released})."
            )
        else:
            said = (
                f"{by} rejected {row.action_type} on {row.target} on "
                f"{_when(utcnow())} ({row.action_id}, {proposed})."
            )
        said += (
            f" Reason: {reason.strip()}"
            if (reason or "").strip()
            else (" No reason was given.")
        )
        with session.begin_nested():
            write_note(
                session,
                entity_key=key,
                statement=said,
                written_by=by,
                actor=principal_id or by,
            )
    except Exception:  # noqa: BLE001 — the decision stands without its note
        logger.exception("reversal note on %s not written", row.action_id)


# ---- The hold ---------------------------------------------------------------


def _kind_of_act(tool: str) -> Optional[str]:
    return ever_carries(tool)


def _undo_reason(row: ApprovalActionRow) -> Optional[str]:
    """The person's reason on an undo row, after "<name> undid <id>: "."""
    said = row.reason or ""
    prefix = f"{row.approved_by} undid {(row.parameters or {}).get('undoes')}: "
    return said[len(prefix) :] if said.startswith(prefix) else None


def _held_on(rows: list, kind: str) -> Optional[str]:
    """Why the newest person's reversal in ``rows`` (one target, oldest first)
    still holds, or None when there is none or a person approved since."""
    reversal = None
    for row in rows:
        if not by_person(row.approved_by, row.approved_by_principal_id):
            continue
        undoes = (row.parameters or {}).get("undoes")
        if undoes and row.status == "rejected":
            continue  # a held undo a person rejected: neither a reversal nor a lift
        if row.status == "rejected" or undoes:
            reversal = row
        elif row.status in ("approved", "executed", "failed") and reversal is not None:
            reversal = None  # a person approved it since: the lift
    if reversal is None:
        return None
    if (reversal.parameters or {}).get("undoes"):
        verb, why = UNDONE_STATE.get(kind, "undid"), _undo_reason(reversal)
    else:
        verb, why = "rejected", reversal.rejection_reason
    return PersonHold(
        f"{reversal.approved_by} {verb} {kind} on {reversal.target} on "
        f"{_when(reversal.approved_at)} ({why.strip() if (why or '').strip() else 'no reason given'}); "
        f"until {_when(reversal.approved_at + HOLD)} only a person may take it there again"
    )


def reversal_held(tool: str, values: Iterable[str]) -> Optional[str]:
    """Why an act a person reversed on this target is left for a person, or None.

    ``tool`` must be a containment act; ``values`` are the call's exact string
    arguments. Rows that can't be read hold the act.
    """
    kind = _kind_of_act(tool)
    if kind is None:
        return None
    targets = sorted({v for v in values if isinstance(v, str) and v})
    if not targets:
        return None
    from core.storage.unit_of_work import unit_of_work

    try:
        with unit_of_work() as session:
            rows = (
                session.execute(
                    select(ApprovalActionRow)
                    .where(
                        ApprovalActionRow.action_type == kind,
                        ApprovalActionRow.target.in_(targets),
                        ApprovalActionRow.approved_at >= utcnow() - HOLD,
                    )
                    .order_by(ApprovalActionRow.approved_at.asc())
                )
                .scalars()
                .all()
            )
            by_target: dict[str, list] = {}
            for row in rows:
                by_target.setdefault(row.target, []).append(row)
            for target in targets:
                if held := _held_on(by_target.get(target, []), kind):
                    return held
    except Exception as exc:  # noqa: BLE001 — no answer is not permission
        logger.error("earlier decisions not read; act held: %s", exc)
        return PersonHold(
            f"a person's earlier decisions on this target could not be read "
            f"({type(exc).__name__})"
        )
    return None
