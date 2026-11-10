"""Open the MCP surface and mint a person an API token, from the host (#4250).

The way to a first token on an install nobody has signed in to, run inside the
backend's environment by an operator with host access::

    python -m core.auth.mcp_token <username> [--label TEXT] [--expires-days N]

    TOKEN=$(docker compose exec -T backend python -m core.auth.mcp_token alice)

The account must be an existing, active person: not a service account, not
deleted. If the MCP surface is closed it is opened first (``config.changed`` on
``mcp.server_enabled``); the token is then minted (``credential.minted``). Both
are on the audit trail by ``system``, and the holder gets a notice without the
token. The token is a ``vgl_api_`` one: it reaches ``/mcp`` and the HTTP API as
its holder, for 90 days unless ``--expires-days`` (1 to 365) says otherwise.

stdout carries the token alone, on one line; everything else goes to stderr.
It is not an API route: nothing reachable over the network can run it.

Exit codes, as ``core.auth.service_account``: 0 done · 64 bad input · 70
database error · 75 database not ready · 77 refused.
"""

from __future__ import annotations

import logging
import sys
from typing import NamedTuple, Optional, TextIO

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from core.audit import trail
from core.auth import mcp_credential_service as tokens
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
from core.storage.models import Principal, User
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

USAGE = (
    "usage: python -m core.auth.mcp_token <username> "
    "[--label TEXT] [--expires-days N]"
)
DEFAULT_LABEL = "minted from shell"
NOTICE_LINK = "/settings?section=users"


class MintRefused(Exception):
    """The named account is not one this command may mint for."""


class NotWritten(Exception):
    """The surface setting or the token was not saved."""


class Holder(NamedTuple):
    user_id: str
    principal_id: str


class Minted(NamedTuple):
    token: str
    credential_id: str
    expires_at: str
    opened: bool


def _parse(argv: list[str]) -> tuple[str, str, Optional[int]]:
    """``(username, label, days)``; ValueError on anything else."""
    username, label, days = None, DEFAULT_LABEL, None
    args = iter(argv)
    for arg in args:
        if arg in ("--label", "--expires-days"):
            value = next(args, None)
            if value is None:
                raise ValueError(f"{arg} needs a value")
            if arg == "--label":
                label = value.strip()
                if not label or len(label) > 200:
                    raise ValueError("a label is 1 to 200 characters")
            else:
                try:
                    days = int(value)
                except ValueError:
                    raise ValueError("--expires-days takes a whole number") from None
        elif arg.startswith("-") or username is not None:
            raise ValueError(f"unexpected argument {arg!r}")
        else:
            username = arg
    if not username:
        raise ValueError("a username is required")
    return username, label, days


def _holder(username: str, days: Optional[int]) -> Holder:
    """The person to mint for, checked as ``recover_admin`` checks, and their lifetime."""
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == username).first()
        principal = session.get(Principal, user.principal_id) if user else None
        if (
            principal is None
            or principal.kind != principals.PERSON
            or principal.deleted_at is not None
        ):
            raise MintRefused(f"{username} is not an existing person")
        if not user.is_active:
            raise MintRefused(f"{username} is inactive")
        tokens.lifetime(user, days)  # ValueError before anything is written
        return Holder(user.user_id, user.principal_id)


def _tell(principal_id: str, label: str, expires_at: str) -> None:
    """As ``POST /api/tokens`` tells its holder. Never the token: notices are emailed."""
    from core.platform.notices import notify

    try:
        notify(
            principal_id,
            "api_token",
            "API token issued",
            f'A token "{label}" was issued for your account from the server\'s '
            f"shell. It expires on {expires_at}.",
            link=NOTICE_LINK,
        )
    except Exception:  # noqa: BLE001 - the token stands; the notice is not its record
        logger.exception("Could not notify the holder of a token change")


def mint_from_shell(username: str, label: str, days: Optional[int]) -> Minted:
    from core.integrations.mcp import surface

    holder = _holder(username, days)
    opened = False
    if not surface.is_enabled():
        if not surface.set_enabled(True, updated_by=trail.SYSTEM):
            raise NotWritten("the surface setting was not saved")
        opened = True
    minted = tokens.mint(holder.user_id, label, days, actor=trail.SYSTEM)
    if minted is None:
        raise NotWritten("the token was not minted")
    expires_at = minted.record.expires_at.strftime("%Y-%m-%d")
    _tell(holder.principal_id, label, expires_at)
    return Minted(minted.token, minted.record.credential_id, expires_at, opened)


def main(
    argv: list[str],
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        username, label, days = _parse(argv)
    except ValueError as exc:
        print(f"error: {exc}\n{USAGE}", file=stderr)
        return EX_USAGE
    try:
        manager = get_db_manager()
        if manager.engine is None:
            manager.initialize()
        minted = mint_from_shell(username, label, days)
    except ValueError as exc:
        print(f"error: {exc}", file=stderr)
        return EX_USAGE
    except MintRefused as exc:
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
    except NotWritten as exc:
        print(f"error: {exc}", file=stderr)
        return EX_SOFTWARE
    if minted.opened:
        print("MCP surface opened.", file=stderr)
    print(
        f"Minted {minted.credential_id} for {username}, expires {minted.expires_at}. "
        "It reaches /mcp and the HTTP API as that user and is shown only once.",
        file=stderr,
    )
    print(minted.token, file=stdout)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
