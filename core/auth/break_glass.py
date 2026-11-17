"""Whether each break-glass account can sign in now (#4533, epic #4526).

The mark itself, and the host commands that set and clear it, are #4532's
(``users.break_glass_at``, ``scripts/reset_password.py``). This module holds the
"can sign in now" test and the bell's condition built on it: a holder of
``users.write`` sees a line for each marked account that fails the test or has
``LOW_CODES`` or fewer codes left. Nothing shows while no account is marked.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.audit.retention import is_erased
from core.auth import password_hash, security_keys
from core.platform import notices
from core.storage.models import Principal, Role, SsoLink, User
from core.storage.unit_of_work import unit_of_work

# The one refusal for codes or tokens on a marked account.
MARKED = "Remove the break-glass mark at the host first"
LOW_CODES = 2
LINK = "/settings?section=users"
# Each fix names the host command; the bell shows plain text.
_HOST = "scripts/reset_password.py"


def codes_left(user: User) -> int:
    """Unspent codes still readable under a live pepper key."""
    return sum(not password_hash.unreadable(c) for c in user.mfa_recovery_codes or [])


def problems(session: Session, user: User) -> list[tuple[str, str]]:
    """What stops this marked account signing in now, each with its fix; empty
    when it can. Low codes are not a problem here: the condition adds them."""
    out: list[tuple[str, str]] = []
    name = user.username
    principal = session.get(Principal, user.principal_id)
    if principal is not None and principal.deleted_at is not None:
        out.append(
            ("it is deleted", f"run {_HOST} --restore {name}, then --reactivate")
        )
    elif not user.is_active:
        out.append(("it is deactivated", f"run {_HOST} --reactivate {name}"))
    if session.query(SsoLink).filter_by(principal_id=user.principal_id).first():
        out.append(
            (
                "it is linked to the identity provider",
                f"run {_HOST} --unmark-break-glass {name}, then invite and mark an "
                "account at an address no identity provider uses",
            )
        )
    role = session.get(Role, user.role_id) if user.role_id else None
    if not (role and (role.permissions or {}).get("roles.manage")):
        out.append(
            (
                "its role can't manage roles",
                "give it a role that can, in Team & access",
            )
        )
    if not user.password_hash or password_hash.unreadable(user.password_hash):
        out.append(
            ("its password can't be read", f"run {_HOST} {name} for a reset link")
        )
    if not codes_left(user) and not security_keys.has_keys(session, user.principal_id):
        out.append(
            ("its recovery codes are spent", f"run {_HOST} --mark-break-glass {name}")
        )
    return out


def _line(session: Session, user: User) -> str | None:
    found = problems(session, user)
    left = codes_left(user)
    name = user.username
    if found:
        what = "; ".join(p for p, _ in found)
        fix = "; ".join(f for _, f in found)
        return f"Break-glass account {name} can't sign in now: {what}. To fix: {fix}."
    if left <= LOW_CODES:
        return (
            f"Break-glass account {name} has {left} recovery "
            f"code{'' if left == 1 else 's'} left. Run {_HOST} --mark-break-glass "
            f"{name} at the host for a fresh set for the safe."
        )
    return None


def condition(user: User) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``users.write``."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "users.write") is not None:
        return []
    with unit_of_work() as session:
        marked = (
            session.query(User)
            .filter(User.break_glass_at.isnot(None))
            .order_by(User.username)
            .all()
        )
        # An erased account signs in as nobody: its mark means nothing.
        lines = [
            _line(session, u)
            for u in marked
            if not is_erased(u.username, u.principal_id)
        ]
    return [{"message": line, "link": LINK} for line in lines if line]


def register() -> None:
    notices.CONDITIONS.append(condition)
