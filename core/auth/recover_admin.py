"""The host's way back: Admin for a named person (#2917), and restricted
cases nobody can read any more (#4363).

Run inside the backend's environment by an operator with host access::

    python -m core.auth.recover_admin <username>
    python -m core.auth.recover_admin --unread-cases
    python -m core.auth.recover_admin --name-reader <case_id> <person>
    python -m core.auth.recover_admin --lift-by <principal> [--since <time>]

``<username>`` gives that person the built-in Admin role. The account must be
an existing, active person: not a service account, not deleted. The change is
on the audit trail as ``user.role_changed`` by ``system``.

``--unread-cases`` lists each restricted case with no active reader: its id,
title, creation time and reader principal ids. It changes nothing.

``--name-reader`` adds one person (username or principal id) to one restricted
case's readers. The same checks as Admin apply: an existing, active, unerased
person. The trail entry is ``case.readers.add`` by ``system``.

``--lift-by`` opens every case whose latest ``case.restrict`` entry on the
audit trail was made by the principal (username or principal id), or by an
agent acting for them; ``--since`` (ISO time, UTC unless it says) keeps only
restrictions from then on. A case someone else restricted later is left alone.
A restrict entry the trail's retention has already removed can't be found, so
its case is not lifted. Each lift is a ``case.unrestrict`` entry by ``system``.

Every use that changes something asks for a reason and refuses an empty one;
the reason is on its trail entry, once the trail exists on this database.
Restricted cases are read straight from the table: this command runs with no
caller bound, outside the read rule. None of this is an API route: nothing
reachable over the network can run it.

Exit codes, as ``core.auth.service_account``: 0 done · 64 bad input · 70
database error · 75 database not ready · 77 refused.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Callable, Optional, TextIO

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.audit import trail
from core.auth import principals
from core.auth.service_account import (
    EX_OK,
    EX_REFUSED,
    EX_SOFTWARE,
    EX_TEMPFAIL,
    EX_USAGE,
    NotReady,
)
from core.storage.connection import get_db_manager
from core.storage.models import Case, Principal, Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

ADMIN_ROLE_ID = "role-admin"
USAGE = """usage: python -m core.auth.recover_admin <username>
       python -m core.auth.recover_admin --unread-cases
       python -m core.auth.recover_admin --name-reader <case_id> <person>
       python -m core.auth.recover_admin --lift-by <principal> [--since <time>]"""


class RecoveryRefused(Exception):
    """The named account or case is not one this command may act on."""


def _granted(role: Optional[Role]) -> list[str]:
    return sorted(
        k for k, v in ((role.permissions if role else None) or {}).items() if v
    )


def _reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise ValueError("a reason is required")
    return reason


def _account(session, name: str) -> Optional[User]:
    """The account with this username, else with this principal id."""
    return (
        session.query(User).filter(User.username == name).first()
        or session.query(User).filter(User.principal_id == name).first()
    )


def _active_person(session, user: Optional[User], name: str) -> User:
    """``user`` when it is an existing, active person; refused otherwise.
    Erasure deactivates the account, so an erased person is refused here."""
    principal = session.get(Principal, user.principal_id) if user else None
    if (
        principal is None
        or principal.kind != principals.PERSON
        or principal.deleted_at is not None
    ):
        raise RecoveryRefused(f"{name} is not an existing person")
    if not user.is_active:
        raise RecoveryRefused(f"{name} is inactive")
    return user


def _append(session, payload: dict) -> None:
    if trail.trail_ready(session):
        trail.append(session, {trail.ACTOR: trail.SYSTEM, **payload})


def recover(username: str, reason: str) -> str:
    """Assign built-in Admin to ``username``. Returns the role it held before."""
    reason = _reason(reason)

    with unit_of_work() as session:
        admin = session.get(Role, ADMIN_ROLE_ID)
        if admin is None:
            raise NotReady(f"role {ADMIN_ROLE_ID} is not seeded yet")
        user = session.query(User).filter(User.username == username).first()
        _active_person(session, user, username)

        previous_role_id = user.role_id
        before = _granted(session.get(Role, previous_role_id))
        user.role_id = ADMIN_ROLE_ID
        session.flush()
        _append(
            session,
            {
                trail.ACTION: "user.role_changed",
                "principal": user.principal_id,
                "role_id": ADMIN_ROLE_ID,
                "previous_role_id": previous_role_id,
                "permissions_before": before,
                "permissions_after": _granted(admin),
                "reason": reason,
            },
        )
        return previous_role_id


def unread_cases() -> list[dict]:
    """Restricted cases with no active reader, oldest first. Changes nothing."""
    from core.cases.case_readers import no_active_reader

    with unit_of_work() as session:
        rows = session.execute(
            select(Case.case_id, Case.title, Case.created_at, Case.reader_principal_ids)
            .where(no_active_reader())
            .order_by(Case.created_at)
        ).all()
        return [
            {
                "case_id": r.case_id,
                "title": r.title,
                "created_at": r.created_at,
                "readers": list(r.reader_principal_ids or []),
            }
            for r in rows
        ]


def name_reader(case_id: str, person: str, reason: str) -> str:
    """Add ``person`` to the readers of restricted case ``case_id``.
    Returns the principal id added."""
    from core.auth.permissions import case_restricted
    from core.cases import case_trail

    reason = _reason(reason)
    with unit_of_work() as session:
        user = _active_person(session, _account(session, person), person)
        case = session.get(Case, case_id, with_for_update=True)
        if case is None:
            raise RecoveryRefused(f"no case {case_id}")
        if not case_restricted(case):
            raise RecoveryRefused(f"{case_id} is not restricted")
        current = list(case.reader_principal_ids or [])
        if user.principal_id in current:
            raise RecoveryRefused(f"{person} already reads {case_id}")
        case.reader_principal_ids = current + [user.principal_id]
        case.updated_at = utcnow()
        session.flush()
        _append(
            session,
            {
                trail.ACTION: case_trail.READERS_ADD,
                "case_id": case_id,
                "added": [user.principal_id],
                "reason": reason,
            },
        )
        return user.principal_id


# The latest case.restrict entry on each case, kept when it is the principal's.
_RESTRICTED_BY = text("""
    SELECT case_id FROM (
        SELECT DISTINCT ON (payload->>'case_id')
               payload->>'case_id' AS case_id,
               payload->>'actor' AS actor,
               payload->>'on_behalf_of' AS on_behalf_of,
               occurred_at
        FROM public.audit_trail
        WHERE payload->>'action' = :action
        ORDER BY payload->>'case_id', seq DESC
    ) latest
    WHERE (actor = :principal OR on_behalf_of = :principal)
      AND (CAST(:since AS timestamptz) IS NULL
           OR occurred_at >= CAST(:since AS timestamptz))
""")


def lift_by(principal: str, since: Optional[datetime], reason: str) -> list[str]:
    """Open every case whose latest restriction ``principal`` made, since
    ``since`` when given. Returns the case ids lifted."""
    from core.cases import case_trail

    reason = _reason(reason)
    with unit_of_work() as session:
        if not trail.trail_ready(session):
            raise NotReady("the audit trail is not on this database yet")
        user = _account(session, principal)
        principal_id = user.principal_id if user else principal
        if session.get(Principal, principal_id) is None:
            raise RecoveryRefused(f"{principal} is not an existing principal")
        params = {
            "action": case_trail.RESTRICT,
            "principal": principal_id,
            "since": since,
        }
        locked = list(
            session.scalars(
                select(Case)
                .where(Case.case_id.in_(list(session.scalars(_RESTRICTED_BY, params))))
                .order_by(Case.case_id)
                .with_for_update()
            )
        )
        # Read again under the locks: a restriction committed meanwhile is someone
        # else's latest, and that case stays as it is.
        still = set(session.scalars(_RESTRICTED_BY, params))
        lifted: list[str] = []
        for case in locked:
            before = list(case.reader_principal_ids or [])
            teams = list(case.reader_team_ids or [])
            if case.case_id not in still or not (before or teams):
                continue
            case.reader_principal_ids = None
            case.reader_team_ids = None
            case.updated_at = utcnow()
            session.flush()
            _append(
                session,
                {
                    trail.ACTION: case_trail.UNRESTRICT,
                    "case_id": case.case_id,
                    "readers": before,
                    **({"teams": teams} if teams else {}),
                    "restricted_by": principal_id,
                    "reason": reason,
                },
            )
            lifted.append(case.case_id)
        return lifted


def _since(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"--since {value!r} is not an ISO time") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _print_unread(stdout: TextIO) -> None:
    rows = unread_cases()
    for row in rows:
        print(
            f"{row['case_id']}\t{row['created_at']:%Y-%m-%d %H:%M}\t"
            f"{row['title']}\treaders: {', '.join(row['readers']) or '-'}",
            file=stdout,
        )
    print(f"{len(rows)} restricted case(s) with no active reader.", file=stdout)


def _parse(argv: list[str]) -> tuple[Optional[str], Callable[[str, TextIO], None]]:
    """``(reason prompt or None, act(reason, stdout))``; None when ``argv`` is
    not a use of this command. A read-only use has no prompt."""
    if argv == ["--unread-cases"]:
        return None, lambda _reason, out: _print_unread(out)
    if len(argv) == 3 and argv[0] == "--name-reader":
        case_id, person = argv[1], argv[2]

        def name(reason: str, out: TextIO) -> None:
            added = name_reader(case_id, person, reason)
            print(f"{case_id}: {added} can now read it.", file=out)

        return f"Reason for naming {person} a reader of {case_id}: ", name
    if argv[:1] == ["--lift-by"] and (
        len(argv) == 2 or (len(argv) == 4 and argv[2] == "--since")
    ):
        principal = argv[1]
        since = _since(argv[3]) if len(argv) == 4 else None

        def lift(reason: str, out: TextIO) -> None:
            lifted = lift_by(principal, since, reason)
            for case_id in lifted:
                print(f"{case_id}: open", file=out)
            print(f"Lifted {len(lifted)} restricted case(s).", file=out)

        return f"Reason for lifting every restriction {principal} made: ", lift
    if len(argv) == 1 and not argv[0].startswith("-"):
        username = argv[0]

        def admin(reason: str, out: TextIO) -> None:
            previous = recover(username, reason)
            print(
                f"{username}: {previous} -> {ADMIN_ROLE_ID}. Sign in again to use it.",
                file=out,
            )

        return f"Reason for giving {username} the Admin role: ", admin
    raise _Usage()


class _Usage(Exception):
    pass


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        prompt, act = _parse(argv)
    except _Usage:
        print(USAGE, file=stderr)
        return EX_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    reason = ""
    if prompt is not None:
        print(prompt, end="", file=stderr)
        stderr.flush()
        reason = stdin.readline()
    try:
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        act(reason, stdout)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except RecoveryRefused as exc:
        print(f"error: {exc}", file=stderr)
        return EX_REFUSED
    except NotReady as exc:
        print(f"database not ready: {exc}", file=stderr)
        return EX_TEMPFAIL
    except OperationalError as exc:
        print(f"database not ready: {type(exc).__name__}", file=stderr)
        return EX_TEMPFAIL
    except SQLAlchemyError as exc:
        print(f"database error: {type(exc).__name__}", file=stderr)
        return EX_SOFTWARE
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
