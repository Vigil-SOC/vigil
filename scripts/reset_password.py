#!/usr/bin/env python3
"""
Recover an account from the host, without email.

    reset_password.py --list                    show every account
    reset_password.py <username|email>          print a one-hour, single-use link
    reset_password.py --reset-mfa <user>        remove the person's two-step sign-in

The link opens the console's /reset-password screen, so the strength policy,
reuse check, lockout clear and session revocation all happen in the existing
/password-reset/confirm route; this script never handles a password. The link
is a row of auth_links, so an administrator can revoke it from the console.

--reset-mfa removes the authenticator app, security keys and recovery codes and
ends the person's sign-ins. It asks for the operator's name and a reason, kept on
the audit trail with this machine's user and host name.

Both tell the person and every holder of users.write. Natively, run it through
scripts/reset-password.sh; on compose, use
``docker exec -it deeptempo-backend python scripts/reset_password.py``.
"""

import argparse
import getpass
import socket
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.audit.trail import SYSTEM  # noqa: E402
from core.auth import invites, security_keys, sign_ins, sso, trail_events  # noqa: E402
from core.auth.auth_service import AuthService, managers  # noqa: E402
from core.auth.identity import matching_accounts  # noqa: E402
from core.auth.password_reset import default_base_url, reset_link  # noqa: E402
from core.auth.password_validator import USERS_LINK  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.platform.notices import notify  # noqa: E402
from core.storage.connection import get_db_manager, init_database  # noqa: E402
from core.storage.models.auth import User  # noqa: E402


def list_users(session) -> int:
    users = session.query(User).order_by(User.username).all()
    if not users:
        print("No users.")
        return 0
    rows = [
        (u.username, u.email, u.role_id, "active" if u.is_active else "inactive")
        for u in users
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[3])
    return 0


def _one(session, who: str):
    """The single account ``who`` names, or None after saying why not."""
    matches = matching_accounts(session, who)
    if not matches:
        print(f"No user with username or email {who!r}. Try --list.", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(
            f"{who!r} matches more than one account. Rename one, then try again.",
            file=sys.stderr,
        )
        for user in sorted(matches, key=lambda item: item.username):
            print(f"  {user.username} <{user.email}>", file=sys.stderr)
        return None
    user = matches[0]
    if user.service_account is True:
        print(
            f"{user.username!r} is a service account: it has no password or "
            "two-step sign-in to recover.",
            file=sys.stderr,
        )
        return None
    return user


def _where() -> dict:
    """Who ran this, and on which machine, for the trail."""
    return {"os_user": getpass.getuser(), "host": socket.gethostname()}


def _tell(principal: str, name: str, title: str, to_person: str, to_managers: str):
    """After the commit: the person and every holder of users.write."""
    notify(principal, "account_recovery", title, to_person, priority="high")
    for to in managers(principal):
        notify(
            to,
            "account_recovery",
            title.replace("Your", f"{name}'s"),
            to_managers,
            USERS_LINK,
            priority="high",
        )


def issue_link(session, who: str, base_url: str):
    user = _one(session, who)
    if user is None:
        return None
    if sso.required():
        print(
            "Single sign-on is required, so reset links are not issued. "
            "Turn it off first: python -m core.auth.require_sso_off",
            file=sys.stderr,
        )
        return None
    if not user.is_active:
        print(
            f"User {user.username!r} is inactive; reactivate it before resetting.",
            file=sys.stderr,
        )
        return None
    # An administrator's link is exempt from the minimum password age.
    _, token = invites.issue_reset(session, user, invites.HOST, SYSTEM, **_where())
    # Printed by main once committed: never a link whose row didn't commit.
    shown = (
        f"Reset link for {user.username} <{user.email}>:\n"
        f"{reset_link(base_url, token)}\nValid for one hour; it works once."
    )
    return user.principal_id, user.username, shown


def _ask(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _has_mfa(session, user) -> bool:
    return bool(
        user.mfa_enabled
        or user.mfa_secret
        or user.mfa_recovery_codes
        or security_keys.has_keys(session, user.principal_id)
    )


def clear_two_step(session, who: str):
    user = _one(session, who)
    if user is None:
        return None
    if not _has_mfa(session, user):
        print(f"{user.username!r} has no two-step sign-in to reset.", file=sys.stderr)
        return None
    # Asked before the row is locked: sign-ins don't wait on the operator's typing.
    session.rollback()
    operator = _ask("Your name: ")
    reason = _ask(f"Reason for resetting {user.username}'s two-step sign-in: ")
    if not operator or not reason:
        print("error: a name and a reason are required", file=sys.stderr)
        return None
    user = (
        session.query(User)
        .filter(User.user_id == user.user_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not _has_mfa(session, user):
        print(f"{user.username!r} has no two-step sign-in to reset.", file=sys.stderr)
        return None
    AuthService.clear_mfa(user)
    keys = security_keys.remove_all(session, user.principal_id)
    ended = sign_ins.end_all_for_user(user.user_id, sign_ins.ENDED_BY_ADMIN, session)
    trail_events.record(
        session,
        "auth.mfa_reset",
        {"operator_text": operator},
        actor=SYSTEM,
        principal=user.principal_id,
        reason=reason,
        security_keys=keys,
        sign_ins_ended=ended,
        **_where(),
    )
    shown = (
        f"{user.username}: two-step sign-in removed, {ended} sign-in"
        f"{'' if ended == 1 else 's'} ended."
    )
    return user.principal_id, user.username, shown


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list every account")
    group.add_argument(
        "--reset-mfa",
        dest="two_step_of",  # not "mfa": CodeQL reads such a name as a secret
        metavar="USER",
        help="remove this person's two-step sign-in",
    )
    group.add_argument("user", nargs="?", help="username or email to reset")
    parser.add_argument("--base-url", help="console URL the link opens")
    args = parser.parse_args(argv)

    init_database(echo=False)
    with get_db_manager().session_scope() as session:
        if args.list:
            return list_users(session)
        if args.two_step_of:
            done = clear_two_step(session, args.two_step_of)
        else:
            base = args.base_url or default_base_url(get_settings())
            done = issue_link(session, args.user, base)
        if done is None:
            session.rollback()
            return 1
    # Committed: now the operator sees it and the people are told (never the link).
    principal, name, shown = done
    print(shown)
    if args.two_step_of:
        _tell(
            principal,
            name,
            "Your two-step sign-in was reset",
            "An administrator on the server removed your two-step sign-in methods "
            "and recovery codes and ended your sign-ins. Set two-step sign-in up "
            "again when you next sign in. If you didn't ask for this, tell an admin.",
            f"An administrator on the server reset {name}'s two-step sign-in.",
        )
    else:
        _tell(
            principal,
            name,
            "Your password reset link",
            "An administrator on the server made a link to reset your password. "
            "It works once, for one hour. If you didn't ask for this, tell an admin.",
            f"An administrator on the server issued a password reset link for {name}.",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
