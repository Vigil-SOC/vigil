"""Give a named person the built-in Admin role, from the host (#2917).

The way back when nobody on an install can manage roles, run inside the
backend's environment by an operator with host access::

    python -m core.auth.recover_admin <username>

It asks for a reason and refuses an empty one. The account must be an
existing, active person: not a service account, not deleted. The change is
on the audit trail as ``user.role_changed`` by ``system``, with the reason and
the permission lists before and after, once the trail exists on this database.
It is not an API route: nothing reachable over the network can run it.

Exit codes, as ``core.auth.service_account``: 0 done · 64 bad input · 70
database error · 75 database not ready · 77 refused.
"""

from __future__ import annotations

import sys
from typing import Optional, TextIO

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
from core.storage.models import Principal, Role, User
from core.storage.unit_of_work import unit_of_work

ADMIN_ROLE_ID = "role-admin"


class RecoveryRefused(Exception):
    """The named account is not one this command may make Admin."""


def _granted(role: Optional[Role]) -> list[str]:
    return sorted(
        k for k, v in ((role.permissions if role else None) or {}).items() if v
    )


def recover(username: str, reason: str) -> str:
    """Assign built-in Admin to ``username``. Returns the role it held before."""
    reason = reason.strip()
    if not reason:
        raise ValueError("a reason is required")

    with unit_of_work() as session:
        admin = session.get(Role, ADMIN_ROLE_ID)
        if admin is None:
            raise NotReady(f"role {ADMIN_ROLE_ID} is not seeded yet")
        user = session.query(User).filter(User.username == username).first()
        principal = session.get(Principal, user.principal_id) if user else None
        if (
            principal is None
            or principal.kind != principals.PERSON
            or principal.deleted_at is not None
        ):
            raise RecoveryRefused(f"{username} is not an existing person")
        if not user.is_active:
            raise RecoveryRefused(f"{username} is inactive")

        previous_role_id = user.role_id
        before = _granted(session.get(Role, previous_role_id))
        user.role_id = ADMIN_ROLE_ID
        session.flush()
        if trail.trail_ready(session):
            trail.append(
                session,
                {
                    trail.ACTION: "user.role_changed",
                    trail.ACTOR: trail.SYSTEM,
                    "principal": user.principal_id,
                    "role_id": ADMIN_ROLE_ID,
                    "previous_role_id": previous_role_id,
                    "permissions_before": before,
                    "permissions_after": _granted(admin),
                    "reason": reason,
                },
            )
        return previous_role_id


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if len(argv) != 1:
        print("usage: python -m core.auth.recover_admin <username>", file=stderr)
        return EX_USAGE
    username = argv[0]
    print(f"Reason for giving {username} the Admin role: ", end="", file=stderr)
    stderr.flush()
    reason = stdin.readline()
    try:
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        previous = recover(username, reason)
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
    print(
        f"{username}: {previous} -> {ADMIN_ROLE_ID}. Sign in again to use it.",
        file=stdout,
    )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
