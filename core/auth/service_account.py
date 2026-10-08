"""Service accounts: machine logins that are not people (D2-17).

Today there is one, Medic's gateway, which reads the backend as a Viewer. The
enable flow for each install shape runs, inside the backend's environment::

    python -m core.auth.service_account ensure medic-<12 of a-z0-9>   < password

The password comes on stdin, never argv or env: ``docker exec`` and ``kubectl
exec`` record the full command line. The account is a Viewer with
``service_account = true``, so failed logins never lock it (a lock would let
anyone who learns the name blind Medic; see ``AuthService._record_failed_attempt``)
and the users API won't give it another role. The name is random so it can't be
guessed; the password is the gateway's alone.

``ensure`` is idempotent: same password → unchanged, new password → rotated
(failure count and any lock cleared). Every run also puts back what a stolen
gateway session could change through ``/api/auth/me``: the email (a reset
address) and MFA (which would lock the gateway out). Any other active service
account is
retired, so a re-minted name leaves no stale login behind. It refuses an
existing account that is not a Viewer service account. Nothing it prints or logs
holds the password.

Exit codes (sysexits, so a caller can tell them from ``docker compose exec``'s
own failures): 0 done · 64 bad input · 70 database error · 77 refused · 75
database not ready (no ``users.service_account`` column or no Viewer role yet;
the caller retries). Errors print their type only: SQLAlchemy's carry the
statement's parameters, which include the password hash.
"""

from __future__ import annotations

import re
import sys
import uuid
from typing import Optional, TextIO

from sqlalchemy import inspect
from sqlalchemy.exc import NoSuchTableError, OperationalError, SQLAlchemyError

from core.auth.auth_service import AuthService
from core.storage.connection import get_db_manager
from core.storage.models import Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

VIEWER_ROLE_ID = "role-viewer"
USERNAME_PATTERN = r"^medic-[a-z0-9]{12}$"
_USERNAME = re.compile(USERNAME_PATTERN)
# 32 bytes floor (D2-17); 72 is bcrypt's ceiling (core/config.py
# auth_max_password_bytes). Printable ASCII without spaces survives every
# login form and file the shapes pass it through.
_PASSWORD = re.compile(r"^[!-~]{32,72}$")

EX_OK = 0
EX_USAGE = 64
EX_SOFTWARE = 70
EX_TEMPFAIL = 75
EX_REFUSED = 77


class ServiceAccountError(Exception):
    """The named account exists and must not be turned into a service account."""


class NotReady(Exception):
    """The schema or the Viewer role isn't there yet; worth retrying."""


def _email(username: str) -> str:
    # Unique and NOT NULL; .invalid never resolves (RFC 2606), so no reset email
    # can go anywhere.
    return f"{username}@service.invalid"


def schema_ready(conn) -> bool:
    """Whether ``users.service_account`` exists (create_all, 40_*.sql or migration)."""
    try:
        columns = inspect(conn).get_columns("users")
    except NoSuchTableError:
        return False  # create_all hasn't run yet

    return any(c["name"] == "service_account" for c in columns)


def ensure(username: str, password: str) -> tuple[str, int]:
    """Create, keep or rotate the service account. Returns (outcome, retired)."""
    if not _USERNAME.fullmatch(username):
        raise ValueError(f"username must match {USERNAME_PATTERN}")
    if not _PASSWORD.fullmatch(password):
        raise ValueError(
            "password must be 32-72 printable ASCII characters with no spaces"
        )

    with get_db_manager().engine.connect() as conn:
        if not schema_ready(conn):
            raise NotReady("users.service_account is missing")

    with unit_of_work() as session:
        if not session.query(Role).filter(Role.role_id == VIEWER_ROLE_ID).first():
            raise NotReady(f"role {VIEWER_ROLE_ID} is not seeded yet")

        user = session.query(User).filter(User.username == username).first()
        now = utcnow()
        if user is None:
            outcome = "created"
            session.add(
                User(
                    user_id=f"user-{uuid.uuid4().hex[:12]}",
                    username=username,
                    email=_email(username),
                    password_hash=AuthService.hash_password(password),
                    full_name="Medic gateway (service account)",
                    role_id=VIEWER_ROLE_ID,
                    is_active=True,
                    is_verified=True,
                    service_account=True,
                    password_changed_at=now,
                )
            )
        else:
            if user.service_account is not True:
                raise ServiceAccountError(
                    f"{username} exists and is not a service account"
                )
            if user.role_id != VIEWER_ROLE_ID:
                raise ServiceAccountError(
                    f"{username} holds {user.role_id}; a service account is Viewer only"
                )
            if AuthService.verify_password(password, user.password_hash):
                outcome = "unchanged"
            else:
                outcome = "rotated"
                user.password_hash = AuthService.hash_password(password)
                user.password_changed_at = now
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
            user.email = _email(username)
            user.mfa_enabled = False
            user.mfa_secret = None
            user.mfa_recovery_codes = []

        retired = 0
        others = session.query(User).filter(
            User.service_account.is_(True),
            User.username != username,
            User.is_active.is_(True),
        )
        for other in others:
            other.is_active = False
            retired += 1
        return outcome, retired


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if len(argv) != 2 or argv[0] != "ensure":
        print(
            "usage: python -m core.auth.service_account ensure <username> < password",
            file=stderr,
        )
        return EX_USAGE
    username = argv[1]
    password = stdin.readline().rstrip("\r\n")
    try:
        # A fresh process: nothing has built the engine yet. Lazy, so a
        # database that isn't up surfaces below as OperationalError (75).
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        outcome, retired = ensure(username, password)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except ServiceAccountError as exc:
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
    line = f"service account {username}: {outcome}"
    if retired:
        line += f" (retired {retired} other)"
    print(line, file=stdout)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
