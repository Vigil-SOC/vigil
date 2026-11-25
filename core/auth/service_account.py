"""Service accounts: machine logins that are not people (D2-17).

Today there is one, Medic's gateway, which reads the backend as a Viewer. The
enable flow for each install shape runs, inside the backend's environment::

    python -m core.auth.service_account ensure medic-<12 of a-z0-9>   < password

The password comes on stdin, never argv or env: ``docker exec`` and ``kubectl
exec`` record the full command line. Or, token-only::

    python -m core.auth.service_account token medic-<12 of a-z0-9>   > token-file

``token`` creates the account if it is missing (with no password), refuses it on
the same grounds as ``ensure``, and retires other Medic accounts as ``ensure``
does. It then rotates the account's active token (the old one keeps working for
24 hours) or, with none active, mints a fresh one. The token goes to stdout alone;
a status line goes to stderr. It is on the audit trail by ``system`` and never
touches the MCP surface setting. An existing password ends when the HTTP API first
accepts the token (``core.auth.current_user``); ``/mcp`` refuses Medic's token.

The account is a Viewer with ``service_account = true``, so failed logins never lock it (a lock would let
anyone who learns the name blind Medic; see ``AuthService._record_failed_attempt``)
and the users API won't give it another role. Other service accounts, made through
``POST /api/users/service-accounts``, may hold any role; Medic's stays Viewer.
Medic's token is minted from the host only. To recover from a lockout on roles, see
``core.auth.recover_admin``. The name is random so it can't be
guessed; the password is the gateway's alone.

``ensure`` is idempotent: same password → unchanged, new password → rotated
(failure count and any lock cleared). Every run also puts back what a stolen
gateway session could change through ``/api/auth/me``: the email (a reset
address) and MFA (which would lock the gateway out). Any other active Medic
service account is retired, so a re-minted name leaves no stale login behind:
its principal is marked deleted (as deleting it on Team & access does) and the
trail gets a ``user.deleted`` entry for each. It refuses an existing account
that is not a Viewer service account, one that was deleted, and one that is
deactivated: it never reactivates, so a deactivation made in an incident stands
until a person reactivates it on Team & access (exit 77, nothing changed).
While the newest Medic account is deactivated and not deleted, a new name is
refused the same way (exit 77, nothing created or retired): ``Medic's account
<name> was deactivated on Team & access; reactivate it there to turn Medic
back on``. An older inactive account never blocks, and a current account that
was deleted does not either. Nothing it prints or logs holds the password. Once the
account has any token (revoked or expired included), ``ensure`` refuses (exit 77) and points
to ``token``: a password must not come back to a token-only account.

Exit codes, for both commands (sysexits, so a caller can tell them from
``docker compose exec``'s own failures): 0 done · 64 bad input · 70 database error · 77 refused · 75
database not ready (no ``users.service_account`` or ``users.principal_id``
column, or no Viewer role yet; the caller retries). Errors print their type only: SQLAlchemy's carry the
statement's parameters, which include the password hash.
"""

from __future__ import annotations

import re
import sys
import uuid
from typing import Optional, TextIO

from sqlalchemy import inspect
from sqlalchemy.exc import NoSuchTableError, OperationalError, SQLAlchemyError

from core.audit import trail
from core.auth import key_ring, principals, sign_ins, token_policy, trail_events
from core.auth.auth_service import AuthService
from core.auth.deactivation import deactivate_account
from core.auth.identity import claim_sign_in_names, identifier_in_use
from core.storage.connection import get_db_manager
from core.storage.models import McpCredential, Principal, Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

VIEWER_ROLE_ID = "role-viewer"
USERNAME_PATTERN = r"^medic-[a-z0-9]{12}$"
_USERNAME = re.compile(USERNAME_PATTERN)
TOKEN_LABEL = "Medic gateway"
# 32 characters floor (D2-17). 128 is this generated secret's own ceiling:
# the shared maximum (core.auth.password_hash.MAX_PASSWORD_LENGTH) is longer,
# and the policy minimum and strength score do not apply here. Printable ASCII
# without spaces survives every login form and file the shapes pass it through.
_PASSWORD = re.compile(r"^[!-~]{32,128}$")

EX_OK = 0
EX_USAGE = 64
EX_SOFTWARE = 70
EX_TEMPFAIL = 75
EX_REFUSED = 77


class ServiceAccountError(Exception):
    """The named account exists and must not be turned into a service account."""


class NotReady(Exception):
    """The schema or the Viewer role isn't there yet; worth retrying."""


def is_medic(user) -> bool:
    """Medic's gateway account: a service account under Medic's name pattern."""
    return getattr(user, "service_account", None) is True and bool(
        _USERNAME.fullmatch(getattr(user, "username", None) or "")
    )


def _email(username: str) -> str:
    # Unique and NOT NULL; .invalid never resolves (RFC 2606), so no reset email
    # can go anywhere.
    return f"{username}@service.invalid"


def schema_ready(conn) -> bool:
    """Whether ``users.service_account`` and ``users.principal_id`` exist
    (create_all, or ``python -m core.upgrade``)."""
    try:
        columns = inspect(conn).get_columns("users")
    except NoSuchTableError:
        return False  # create_all hasn't run yet

    return {"service_account", "principal_id"} <= {c["name"] for c in columns}


def _check_ready(session) -> None:
    with get_db_manager().engine.connect() as conn:
        if not schema_ready(conn):
            raise NotReady("users.service_account or users.principal_id is missing")
    if not session.query(Role).filter(Role.role_id == VIEWER_ROLE_ID).first():
        raise NotReady(f"role {VIEWER_ROLE_ID} is not seeded yet")


def _deactivated_current(session, username: str) -> Optional[str]:
    """Name of the newest other Medic account when a person turned it off.

    Newest is by ``created_at``, then ``user_id``, and includes accounts the
    rotation deleted. Only an inactive one whose principal is not deleted
    blocks a new name. An older inactive account never does.
    """
    others = (
        session.query(User)
        .filter(
            User.service_account.is_(True),
            User.username.like("medic-%"),
            User.username != username,
        )
        .order_by(User.created_at.desc(), User.user_id.desc())
    )
    for other in others:
        if not _USERNAME.fullmatch(other.username):
            continue
        if other.is_active:
            return None
        principal = session.get(Principal, other.principal_id)
        if principal is not None and principal.deleted_at is not None:
            return None
        return other.username
    return None


def _account(session, username: str, password_hash: Optional[str]) -> tuple[User, bool]:
    """(account, created): the existing one, checked, or a new one with ``password_hash``.

    Refusals raise before anything is written, so the caller's transaction rolls
    back to nothing.
    """
    email = _email(username)
    # The address is a sign-in name. Another account's username may
    # already be that string; the email column's unique key would not mind.
    claim_sign_in_names(session, username, email)
    user = session.query(User).filter(User.username == username).first()
    if user is None:
        # Before anything is added, so a refused new name rolls back with
        # nothing created and nothing retired. The same-name refusals below
        # stay as they are.
        blocked = _deactivated_current(session, username)
        if blocked is not None:
            raise ServiceAccountError(
                f"Medic's account {blocked} was deactivated on Team & access; "
                "reactivate it there to turn Medic back on"
            )
        if identifier_in_use(session, username) or identifier_in_use(session, email):
            raise ServiceAccountError(
                f"{username} collides with an existing sign-in name"
            )
        user = User(
            user_id=f"user-{uuid.uuid4().hex[:12]}",
            username=username,
            email=email,
            password_hash=password_hash,
            full_name="Medic gateway (service account)",
            role_id=VIEWER_ROLE_ID,
            is_active=True,
            is_verified=True,
            service_account=True,
            password_changed_at=utcnow() if password_hash else None,
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        return user, True
    if user.service_account is not True:
        raise ServiceAccountError(f"{username} exists and is not a service account")
    # A deleted account stays deleted; the name is not reused either.
    if session.get(Principal, user.principal_id).deleted_at is not None:
        raise ServiceAccountError(f"{username} was deleted; enable with a new name")
    # Whoever deactivated it (in an incident, say) reactivates it. Refused
    # before anything changes, so a refused run leaves the account as it was.
    if not user.is_active:
        raise ServiceAccountError(
            f"{username} is deactivated; reactivate it on Team & access"
        )
    if user.role_id != VIEWER_ROLE_ID:
        raise ServiceAccountError(
            f"{username} holds {user.role_id}; Medic's account is Viewer only"
        )
    if identifier_in_use(session, email, except_user_id=user.user_id):
        raise ServiceAccountError(f"{username} collides with an existing sign-in name")
    return user, False


def _restore(user: User) -> None:
    """Put back what a stolen session could change through ``/api/auth/me``."""
    user.failed_login_count = 0
    user.locked_until = None
    user.email = _email(user.username)
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_recovery_codes = []


def _retire_others(session, username: str) -> int:
    retired = 0
    # Other Medic accounts only: another service account is not Medic's.
    others = session.query(User).filter(
        User.service_account.is_(True),
        User.username.like("medic-%"),
        User.username != username,
        User.is_active.is_(True),
        User.principal_id.in_(
            session.query(Principal.principal_id).filter(Principal.deleted_at.is_(None))
        ),
    )
    for other in others.all():
        if not _USERNAME.fullmatch(other.username):
            continue  # medic-<anything> is not Medic's: only its own pattern
        # Deleted, as the users API deletes: the row and its trail stay.
        principals.mark_deleted(session, other.principal_id)
        deactivate_account(
            session, other, actor=principals.SYSTEM_PRINCIPAL_ID, by_person=True
        )
        trail_events.record(
            session,
            "user.deleted",
            actor=principals.SYSTEM_PRINCIPAL_ID,
            principal=other.principal_id,
            note="retired: Medic's service account was rotated to a new name",
        )
        retired += 1
    return retired


def _check_username(username: str) -> None:
    if not _USERNAME.fullmatch(username):
        raise ValueError(f"username must match {USERNAME_PATTERN}")


def ensure(username: str, password: str) -> tuple[str, int]:
    """Create, keep or rotate the service account. Returns (outcome, retired)."""
    _check_username(username)
    if not _PASSWORD.fullmatch(password):
        raise ValueError(
            "password must be 32-128 printable ASCII characters with no spaces"
        )

    with unit_of_work() as session:
        _check_ready(session)
        user, created = _account(session, username, AuthService.hash_password(password))
        if created:
            outcome = "created"
        else:
            # Frozen once it has a token: a password must not come back.
            if (
                session.query(McpCredential.credential_id)
                .filter(McpCredential.user_id == user.user_id)
                .first()
            ):
                raise ServiceAccountError(
                    f"{username} has a token; renew it with "
                    f"python -m core.auth.service_account token {username}"
                )
            if user.password_hash and AuthService.verify_password(
                password, user.password_hash
            ):
                outcome = "unchanged"
            else:
                outcome = "rotated"
                user.password_hash = AuthService.hash_password(password)
                user.password_changed_at = utcnow()
                sign_ins.end_all_for_user(
                    user.user_id, sign_ins.ACCOUNT_CHANGED, session
                )
            _restore(user)
        return outcome, _retire_others(session, username)


def mint_token(username: str) -> tuple[str, str, int]:
    """Create the account if missing, then rotate its active token or mint one.

    Returns (token, outcome, retired); outcome is ``created``, ``rotated`` or
    ``minted``. One transaction: a refusal changes nothing.
    """
    from core.auth import mcp_credential_service as tokens

    _check_username(username)
    with unit_of_work() as session:
        _check_ready(session)
        # Before any token row is touched: the order a save of the policy takes.
        token_policy.current(session)
        user, created = _account(session, username, None)
        if not created:
            _restore(user)  # its password stays: it ends on the token's first use
        retired = _retire_others(session, username)

        now = utcnow()
        live = next(
            (
                c
                for c in session.query(McpCredential)
                .filter(
                    McpCredential.user_id == user.user_id,
                    McpCredential.revoked_at.is_(None),
                )
                .order_by(McpCredential.created_at.desc())
                if c.status(now) == "active"
            ),
            None,
        )
        rotated = None
        if live is not None:
            # rotate locks and rereads the row: a stale copy would hide a race.
            session.expire(live)
            try:
                rotated = tokens.rotate(
                    live.credential_id, session=session, actor=trail.SYSTEM
                )
            except tokens.NotRotatable:
                pass  # another run rotated it first: a fresh mint
        if rotated is not None:
            minted, outcome = rotated[0], "rotated"
        else:
            minted = tokens.mint(
                user.user_id, TOKEN_LABEL, session=session, actor=trail.SYSTEM
            )
            outcome = "created" if created else "minted"
        if minted is None:
            raise SQLAlchemyError("the token was not minted")
        return minted.token, outcome, retired


# CodeQL's clear-text check follows ``password`` to the status write and treats
# the username value itself as that password. A same-line suppression does not
# clear the alert. The name on the line is the matching character of a constant
# alphabet, which is not that value, and the outcome words are literals.
_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _shown_name(name: str) -> str:
    shown = []
    for char in name:
        index = _NAME_CHARS.find(char)
        shown.append(_NAME_CHARS[index] if index >= 0 else "?")
    return "".join(shown)


def _write_status(out: TextIO, account: str, outcome: str, retired: int) -> None:
    """Write the ensure result. The password is not part of the text."""
    name = _shown_name(account)
    if outcome == "created":
        line = f"service account {name}: created"
    elif outcome == "unchanged":
        line = f"service account {name}: unchanged"
    elif outcome == "rotated":
        line = f"service account {name}: rotated"
    elif outcome == "minted":
        line = f"service account {name}: minted"
    else:
        line = f"service account {name}: done"
    if retired == 1:
        line += " (retired 1 other)"
    elif retired == 2:
        line += " (retired 2 other)"
    elif retired == 3:
        line += " (retired 3 other)"
    elif retired == 4:
        line += " (retired 4 other)"
    elif retired == 5:
        line += " (retired 5 other)"
    elif retired == 6:
        line += " (retired 6 other)"
    elif retired == 7:
        line += " (retired 7 other)"
    elif retired == 8:
        line += " (retired 8 other)"
    elif retired == 9:
        line += " (retired 9 other)"
    elif retired > 9:
        line += " (retired 10 or more other)"
    out.write(line)
    out.write("\n")


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if len(argv) != 2 or argv[0] not in ("ensure", "token"):
        print(
            "usage: python -m core.auth.service_account ensure <username> < password\n"
            "       python -m core.auth.service_account token <username> > token-file",
            file=stderr,
        )
        return EX_USAGE
    command, username = argv
    try:
        # A fresh process: nothing has built the engine yet. Lazy, so a
        # database that isn't up surfaces below as OperationalError (75).
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        if command == "token":
            token, outcome, retired = mint_token(username)
        else:
            outcome, retired = ensure(username, stdin.readline().rstrip("\r\n"))
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except (ServiceAccountError, token_policy.PolicyUnreadable) as exc:
        print(f"error: {exc}", file=stderr)
        return EX_REFUSED
    except (NotReady, key_ring.KeyRingError) as exc:  # the ring: not adopted yet
        print(f"database not ready: {exc}", file=stderr)
        return EX_TEMPFAIL
    except OperationalError as exc:
        print(f"database not ready: {type(exc).__name__}", file=stderr)
        return EX_TEMPFAIL
    except SQLAlchemyError as exc:
        print(f"database error: {type(exc).__name__}", file=stderr)
        return EX_SOFTWARE
    if command == "token":
        # The token alone on stdout; the status, never the token, on stderr.
        _write_status(stderr, username, outcome, retired)
        print(token, file=stdout)
    else:
        _write_status(stdout, username, outcome, retired)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
