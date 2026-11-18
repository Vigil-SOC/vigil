"""Turn directory sync off, from the host (#4423).

The way back when the push should stop and sign-in should create accounts
again, run inside the backend's environment by an operator with host access::

    python -m core.auth.directory_sync_off

It asks for a reason and refuses an empty one. It sets ``sync_enabled`` false
in the ``auth.sso`` settings and bumps their version. The directory sync token
is left in place, and the output says where it now stands under the unused-token
rule and when its first reminder would come. The change is on the audit trail as ``directory.sync_off``
by ``system``, with the reason, once the trail exists on this database. It is
not an API route: nothing reachable over the network can run it.

Exit codes, as ``core.auth.service_account``: 0 done · 64 bad input · 70
database error · 75 database not ready · 77 refused (it was not on).
"""

from __future__ import annotations

import sys
from typing import Optional, TextIO

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.auth.directory_sync import SyncRefused, turn_off, unused_rule_text
from core.auth.service_account import (
    EX_OK,
    EX_REFUSED,
    EX_SOFTWARE,
    EX_TEMPFAIL,
    EX_USAGE,
)
from core.storage.connection import get_db_manager
from core.storage.unit_of_work import unit_of_work


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
        print("usage: python -m core.auth.directory_sync_off", file=stderr)
        return EX_USAGE
    print("Reason for turning off directory sync: ", end="", file=stderr)
    stderr.flush()
    reason = stdin.readline()
    try:
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        with unit_of_work() as session:
            version = turn_off(session, reason)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except SyncRefused as exc:
        print(f"error: {exc.detail}", file=stderr)
        return EX_REFUSED
    except OperationalError as exc:
        print(f"database not ready: {type(exc).__name__}", file=stderr)
        return EX_TEMPFAIL
    except SQLAlchemyError as exc:
        print(f"database error: {type(exc).__name__}", file=stderr)
        return EX_SOFTWARE
    # After the switch-off has committed: the rule reads sync as off.
    try:
        with unit_of_work() as session:
            rule = unused_rule_text(session)
    except SQLAlchemyError:
        rule = "The unused-token rule could not be read, so no first reminder date is given."
    print(
        f"Directory sync is off (settings v{version}). "
        "Sign-in can create accounts again. The directory sync token was left in place. "
        + rule,
        file=stdout,
    )
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
