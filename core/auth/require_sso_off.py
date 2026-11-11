"""Turn "Require single sign-on" off, from the host (#4286).

The way back when the identity provider is down and nobody can sign in, run
inside the backend's environment by an operator with host access::

    python -m core.auth.require_sso_off

It asks for a reason and refuses an empty one. It sets ``require_sso`` false in
the ``auth.sso`` settings and bumps their version; password sign-in and reset
links work again at once. The change is on the audit trail as
``sso.required_off`` by ``system``, with the reason, once the trail exists on
this database. It is not an API route: nothing reachable over the network can run it.

Exit codes, as ``core.auth.service_account``: 0 done · 64 bad input · 70
database error · 75 database not ready · 77 refused (it was not on).
"""

from __future__ import annotations

import sys
from typing import Optional, TextIO

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.audit import trail
from core.auth import sso_settings
from core.auth.service_account import (
    EX_OK,
    EX_REFUSED,
    EX_SOFTWARE,
    EX_TEMPFAIL,
    EX_USAGE,
)
from core.storage.connection import get_db_manager
from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow


class AlreadyOff(Exception):
    """Require single sign-on is not on: nothing to turn off."""


def turn_off(reason: str) -> int:
    """Set ``require_sso`` false. Returns the new settings version."""
    reason = reason.strip()
    if not reason:
        raise ValueError("a reason is required")

    with unit_of_work() as session:
        old, found = sso_settings.read_row(session, lock=True)
        if not found or not old.settings.require_sso:
            raise AlreadyOff("Require single sign-on is already off")
        version = old.version + 1
        new = old.settings.model_copy(update={"require_sso": False})
        row = session.get(SystemConfig, sso_settings.KEY)
        # updated_by stays: it names a user, and system is not one.
        row.value = {**new.model_dump(mode="json"), "version": version}
        row.updated_at = utcnow()
        session.flush()
        if trail.trail_ready(session):
            trail.append(
                session,
                {
                    trail.ACTION: "sso.required_off",
                    trail.ACTOR: trail.SYSTEM,
                    "version": version,
                    "reason": reason,
                },
            )
        return version


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if argv:
        print("usage: python -m core.auth.require_sso_off", file=stderr)
        return EX_USAGE
    print("Reason for turning off Require single sign-on: ", end="", file=stderr)
    stderr.flush()
    reason = stdin.readline()
    try:
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        version = turn_off(reason)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except AlreadyOff as exc:
        print(f"error: {exc}", file=stderr)
        return EX_REFUSED
    except OperationalError as exc:
        print(f"database not ready: {type(exc).__name__}", file=stderr)
        return EX_TEMPFAIL
    except SQLAlchemyError as exc:
        print(f"database error: {type(exc).__name__}", file=stderr)
        return EX_SOFTWARE
    print(
        f"Require single sign-on is off (settings v{version}). "
        "People can sign in with their password again.",
        file=stdout,
    )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
