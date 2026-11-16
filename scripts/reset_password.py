#!/usr/bin/env python3
"""
Recover an account from the host, without email.

    reset_password.py --list
    reset_password.py <username|email> [--base-url URL] [--suspected-compromise]
    reset_password.py --reset-mfa <user> [--suspected-compromise]
    reset_password.py --reactivate <user> [--suspected-compromise]
    reset_password.py --restore <user> [--suspected-compromise]

Each act asks for your name, a reason, and how you confirmed the person
(in person, a video call, a call back to a number on file, or another
verified channel). It records those with this machine's account, the
invoking user when run under sudo, and the host. --suspected-compromise
also revokes their API tokens.

The link opens the console's /reset-password screen. Nothing is printed
for localhost: set VIGIL_FRONTEND_URL, pass --base-url, or type the public
address when asked. A restore comes back deactivated and prints no link.

Natively, run it through scripts/reset-password.sh; on compose, use
``docker exec -it deeptempo-backend python scripts/reset_password.py``.
"""

import argparse
import getpass
import os
import socket
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.audit.retention import is_erased  # noqa: E402
from core.audit.trail import SYSTEM  # noqa: E402
from core.auth import (  # noqa: E402
    invites,
    mcp_credential_service,
    security_keys,
    sign_ins,
    sso,
    trail_events,
)
from core.auth.auth_service import (  # noqa: E402
    AuthService,
    clear_lock,
    managers,
    with_dispute,
)
from core.auth.deactivation import clear_deactivation  # noqa: E402
from core.auth.identity import matching_accounts  # noqa: E402
from core.auth.password_reset import reset_link  # noqa: E402
from core.auth.password_validator import USERS_LINK  # noqa: E402
from core.auth.principals import restore as restore_principal  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.platform.notices import notify  # noqa: E402
from core.storage.connection import get_db_manager, init_database  # noqa: E402
from core.storage.models import Principal, User  # noqa: E402
from core.teams import join_default_if_none  # noqa: E402
from core.time import utcnow  # noqa: E402

# The console's IdentityConfirmedBy values, and the words the prompt shows.
METHODS = {
    "in_person": "in person",
    "video_call": "on a video call",
    "phone_call_back": "by calling them back on a number on file",
    "other_verified_channel": "another verified channel",
}


class _Operator:
    """Who ran a host act. The name is readable; the rest is erased with it."""

    def __init__(self, name, reason, method, note, behalf, compromise):
        self.name = name
        self.reason = reason
        self.method = method
        self.note = note
        self.behalf = behalf
        self.compromise = compromise

    def beside(self) -> dict:
        personal = {
            "reason": self.reason,
            "identity_confirmed_by": self.method,
            "os_user": getpass.getuser(),
            "host": socket.gethostname(),
        }
        if self.note:
            personal["verification_note"] = self.note
        invoking = os.environ.get("SUDO_USER", "").strip()
        if invoking:
            personal["invoking_user"] = invoking
        return {"operator_text": self.name, "personal": personal}

    def hashed(self) -> dict:
        """Ids and the compromise choice. Strings that name a person stay beside."""
        fields = {}
        if self.behalf:
            fields["on_behalf_of"] = self.behalf
        if self.compromise:
            fields["suspected_compromise"] = True
        return fields


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


def _principal(session, user):
    return session.get(Principal, user.principal_id)


def _is_deleted(session, user) -> bool:
    principal = _principal(session, user)
    return principal is not None and principal.deleted_at is not None


def _say_erased(user) -> bool:
    if is_erased(user.username, user.principal_id):
        print(f"{user.username!r} is erased and cannot be recovered.", file=sys.stderr)
        return True
    return False


def _tell(
    principal: str,
    name: str,
    title: str,
    to_person: str,
    to_managers: str,
    fallback: str = "",
):
    """After the commit: the person and every holder of users.write."""
    to_person = with_dispute(principal, to_person, fallback)
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


def _ask(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr)
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _behalf(session, who: str):
    """The operator's principal, '' when they left it blank, None to refuse."""
    who = who.strip()
    if not who:
        return ""
    matches = matching_accounts(session, who)
    if len(matches) > 1:
        print(
            f"{who!r} matches more than one account. Leave it blank, or name one.",
            file=sys.stderr,
        )
        return None
    user = matches[0] if matches else None
    principal = user and session.get(Principal, user.principal_id)
    if (
        user is None
        or user.service_account is True
        or not user.is_active
        or principal is None
        or principal.deleted_at is not None
        or is_erased(user.username, user.principal_id)
    ):
        print(f"No active account named {who!r}.", file=sys.stderr)
        return None
    return user.principal_id


def _ask_operator(session, compromise: bool):
    """Name, reason and verification, before any row changes. None refuses."""
    name = _ask("Your name: ")
    if not name:
        print("error: a name is required", file=sys.stderr)
        return None
    reason = _ask("Reason: ")
    if not reason:
        print("error: a reason is required", file=sys.stderr)
        return None
    method = _ask(
        "How you confirmed them:\n"
        + "".join(f"  {key} — {words}\n" for key, words in METHODS.items())
        + "Verification method: "
    )
    if not method:
        print("error: a verification method is required", file=sys.stderr)
        return None
    if method not in METHODS:
        print(
            "error: verification method must be one of " + ", ".join(METHODS),
            file=sys.stderr,
        )
        return None
    note = ""
    if method == "other_verified_channel":
        note = _ask("Note for that channel: ")
        if not note:
            print("error: that verification method needs a note", file=sys.stderr)
            return None
    behalf = _behalf(
        session, _ask("Your Vigil account (username or email), or leave blank: ")
    )
    if behalf is None:
        return None
    return _Operator(name, reason, method, note, behalf or None, compromise)


def _lock(session, user_id: str):
    return (
        session.query(User)
        .filter(User.user_id == user_id)
        .with_for_update()
        .populate_existing()
        .first()
    )


def _open(session, who: str):
    """The account, still unlocked. None when it is missing, a service account or erased."""
    user = _one(session, who)
    if user is None or _say_erased(user):
        return None
    return user


def _operator_then_lock(session, user_id: str, compromise: bool):
    """Prompts, then the locked row. None refuses, and nothing has been written."""
    op = _ask_operator(session, compromise)
    if op is None:
        return None
    user = _lock(session, user_id)
    if user is None or user.service_account is True or _say_erased(user):
        return None
    return user, op


def _has_mfa(session, user) -> bool:
    return bool(
        user.mfa_enabled
        or user.mfa_secret
        or user.mfa_recovery_codes
        or security_keys.has_keys(session, user.principal_id)
    )


def _revoke(session, user, compromised: bool) -> int:
    """Live API tokens. ``compromised`` marks each credential.revoked entry."""
    revoked = 0
    for credential in mcp_credential_service.list_for_user(
        user.user_id, session=session
    ):
        if not mcp_credential_service.revoke(credential.credential_id, session=session):
            continue
        trail_events.record(
            session,
            "credential.revoked",
            actor=SYSTEM,
            principal=user.principal_id,
            credential_id=credential.credential_id,
            compromised=True if compromised else None,
        )
        revoked += 1
    return revoked


def _token_line(count: int) -> str:
    noun = "API token" if count == 1 else "API tokens"
    return f"{count} {noun} revoked."


def _record(session, action: str, user, op: _Operator, **fields) -> None:
    trail_events.record(
        session,
        action,
        op.beside(),
        actor=SYSTEM,
        principal=user.principal_id,
        **op.hashed(),
        **fields,
    )


def _base_url(given: str | None) -> str | None:
    """The address the operator named. Never a guessed localhost."""
    if given:
        return given
    configured = (get_settings().vigil_frontend_url or "").strip()
    if configured:
        return configured
    typed = _ask("Public address of the console: ")
    if not typed:
        print(
            "error: no public address is set; no link was issued",
            file=sys.stderr,
        )
        return None
    return typed


def _link_blocked(session, user) -> bool:
    """True when this account must not be given a link. Said, and nothing written."""
    if sso.required():
        print(
            "Single sign-on is required, so reset links are not issued. "
            "Turn it off first: python -m core.auth.require_sso_off",
            file=sys.stderr,
        )
        return True
    if _is_deleted(session, user):
        print(
            f"User {user.username!r} is deleted; restore it, then reactivate it, "
            "before resetting.",
            file=sys.stderr,
        )
        return True
    if not user.is_active:
        print(
            f"User {user.username!r} is inactive; reactivate it before resetting.",
            file=sys.stderr,
        )
        return True
    return False


def issue_link(session, who: str, base_url: str | None, compromise: bool):
    user = _open(session, who)
    if user is None or _link_blocked(session, user):
        return None
    user_id = user.user_id
    session.rollback()
    op = _ask_operator(session, compromise)
    if op is None:
        return None
    # Before the lock and before issue_reset: an empty address issues nothing.
    base = _base_url(base_url)
    if base is None:
        return None
    user = _lock(session, user_id)
    if (
        user is None
        or user.service_account is True
        or _say_erased(user)
        or _link_blocked(session, user)
    ):
        return None
    # An administrator's link is exempt from the minimum password age.
    _, token = invites.issue_reset(
        session, user, invites.HOST, SYSTEM, beside=op.beside(), **op.hashed()
    )
    revoked = _revoke(session, user, True) if op.compromise else 0
    shown = (
        f"Reset link for {user.username} <{user.email}>:\n"
        f"{reset_link(base, token)}\nValid for one hour; it works once."
    )
    if op.compromise:
        shown = f"{shown}\n{_token_line(revoked)}"
    return user.principal_id, user.username, shown


def clear_two_step(session, who: str, compromise: bool):
    user = _open(session, who)
    if user is None:
        return None
    if not _has_mfa(session, user):
        print(f"{user.username!r} has no two-step sign-in to reset.", file=sys.stderr)
        return None
    user_id = user.user_id
    session.rollback()
    found = _operator_then_lock(session, user_id, compromise)
    if found is None:
        return None
    user, op = found
    if not _has_mfa(session, user):
        print(f"{user.username!r} has no two-step sign-in to reset.", file=sys.stderr)
        return None
    AuthService.clear_mfa(user)
    keys = security_keys.remove_all(session, user.principal_id)
    ended = sign_ins.end_all_for_user(user.user_id, sign_ins.ENDED_BY_ADMIN, session)
    _record(
        session,
        "auth.mfa_reset",
        user,
        op,
        security_keys=keys,
        sign_ins_ended=ended,
    )
    revoked = _revoke(session, user, True) if op.compromise else 0
    shown = (
        f"{user.username}: two-step sign-in removed, {ended} sign-in"
        f"{'' if ended == 1 else 's'} ended."
    )
    if op.compromise:
        shown = f"{shown}\n{_token_line(revoked)}"
    return user.principal_id, user.username, shown


def _reactivate_blocked(session, user) -> bool:
    if _is_deleted(session, user):
        print(
            f"{user.username!r} is deleted; restore it before reactivating.",
            file=sys.stderr,
        )
        return True
    if user.is_active:
        print(f"{user.username!r} is already active.", file=sys.stderr)
        return True
    return False


def reactivate(session, who: str, compromise: bool):
    user = _open(session, who)
    if user is None or _reactivate_blocked(session, user):
        return None
    user_id = user.user_id
    session.rollback()
    found = _operator_then_lock(session, user_id, compromise)
    if found is None:
        return None
    user, op = found
    if _reactivate_blocked(session, user):
        return None
    user.is_active = True
    clear_deactivation(user)
    # Back in use: the idle clock starts again and a lock ends.
    user.dormancy_floor = utcnow()
    clear_lock(user)
    session.flush()
    _record(session, "user.enabled", user, op)
    join_default_if_none(session, user.principal_id, actor=SYSTEM)
    revoked = _revoke(session, user, True) if op.compromise else 0
    shown = (
        f"{user.username}: reactivated. The password and two-step sign-in are "
        "unchanged, and tokens already revoked stay revoked."
    )
    if op.compromise:
        shown = f"{shown}\n{_token_line(revoked)}"
    return user.principal_id, user.username, shown


def restore(session, who: str, compromise: bool):
    user = _open(session, who)
    if user is None:
        return None
    if not _is_deleted(session, user):
        print(f"{user.username!r} is not deleted.", file=sys.stderr)
        return None
    user_id = user.user_id
    session.rollback()
    found = _operator_then_lock(session, user_id, compromise)
    if found is None:
        return None
    user, op = found
    principal = session.get(Principal, user.principal_id, with_for_update=True)
    if principal is None or principal.deleted_at is None:
        print(f"{user.username!r} is not deleted.", file=sys.stderr)
        return None
    restore_principal(principal)
    user.is_active = False
    session.flush()
    _record(session, "user.restored", user, op)
    # Deleting revoked them; one left live is revoked now. Compromise marks it.
    revoked = _revoke(session, user, op.compromise)
    shown = (
        f"{user.username}: restored, and left deactivated. The password and "
        "two-step sign-in are unchanged. No reset link was issued."
    )
    if op.compromise:
        shown = f"{shown}\n{_token_line(revoked)}"
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
    group.add_argument(
        "--reactivate",
        metavar="USER",
        help="reactivate a deactivated account; password and two-step sign-in stay",
    )
    group.add_argument(
        "--restore",
        metavar="USER",
        help="restore a deleted account; it comes back deactivated, with no link",
    )
    group.add_argument("user", nargs="?", help="username or email to reset")
    parser.add_argument("--base-url", help="console URL the link opens")
    parser.add_argument(
        "--suspected-compromise",
        action="store_true",
        help="revoke this person's API tokens and record that compromise is suspected",
    )
    args = parser.parse_args(argv)
    if args.list and args.suspected_compromise:
        print(
            "error: suspected compromise applies to a recovery, not --list",
            file=sys.stderr,
        )
        return 1

    init_database(echo=False)
    with get_db_manager().session_scope() as session:
        if args.list:
            return list_users(session)
        if args.two_step_of:
            done = clear_two_step(session, args.two_step_of, args.suspected_compromise)
        elif args.reactivate:
            done = reactivate(session, args.reactivate, args.suspected_compromise)
        elif args.restore:
            done = restore(session, args.restore, args.suspected_compromise)
        else:
            done = issue_link(
                session, args.user, args.base_url, args.suspected_compromise
            )
        if done is None:
            session.rollback()
            return 1
    # Committed: now the operator sees it and the people are told (never the link).
    principal, name, shown = done
    print(shown)
    extra = ""
    managers_extra = ""
    if args.suspected_compromise:
        extra = " Every API token you held was revoked."
        managers_extra = " Their API tokens were revoked."
    if args.two_step_of:
        _tell(
            principal,
            name,
            "Your two-step sign-in was reset",
            "An administrator on the server removed your two-step sign-in methods "
            "and recovery codes and ended your sign-ins. Set two-step sign-in up "
            "again when you next sign in."
            + extra
            + " If you didn't ask for this, tell an admin.",
            f"An administrator on the server reset {name}'s two-step sign-in."
            + managers_extra,
            "If you didn't ask for this, tell an admin.",
        )
    elif args.reactivate:
        _tell(
            principal,
            name,
            "Your account was reactivated",
            "An administrator on the server reactivated your account. Sign in with "
            "your password and two-step sign-in as before. MCP tokens from before "
            "stay revoked: mint new ones." + extra,
            f"An administrator on the server reactivated {name}." + managers_extra,
            "",
        )
    elif args.restore:
        _tell(
            principal,
            name,
            "Your account was restored",
            "An administrator on the server restored your deleted account. It stays "
            "deactivated until an administrator reactivates it; MCP tokens from "
            "before stay revoked." + extra,
            f"An administrator on the server restored {name}'s deleted account."
            + managers_extra,
            "",
        )
    else:
        _tell(
            principal,
            name,
            "Your password reset link",
            "An administrator on the server made a link to reset your password. "
            "It works once, for one hour."
            + extra
            + " If you didn't ask for this, tell an admin.",
            f"An administrator on the server issued a password reset link for {name}."
            + managers_extra,
            "If you didn't ask for this, tell an admin.",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
