"""People who can sign in without MFA while the policy does not require it of everyone (#4177).

The count uses the policy's own coverage check at the "everyone" level. It feeds
a live warning for those who manage sessions and a daily entry on the audit trail.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from core.auth import security_keys, session_policy, trail_events
from core.platform import notices
from core.storage.models import Role, SecurityKey, SsoLink, User
from core.storage.unit_of_work import unit_of_work

ACTION = "auth.mfa_coverage_gap"
ACTOR = "system"
# Advisory lock key: replicas take turns at the once-a-day check.
LOCK = 4177
SETTINGS_LINK = "/settings?section=sessions"


def _signs_in_by_password():
    """Leaves out an account with no password and a single sign-on link: it signs
    in only through the provider, which carries the MFA (#4284)."""
    linked = select(SsoLink.link_id).where(SsoLink.principal_id == User.principal_id)
    return or_(func.coalesce(User.password_hash, "") != "", ~linked.exists())


def without_mfa(session: Session, level: str = "everyone") -> int:
    """Active accounts ``level`` would cover that have no MFA yet."""
    users = (
        session.query(User.service_account, Role.permissions)
        .outerjoin(Role, Role.role_id == User.role_id)
        .filter(
            User.is_active.is_(True),
            User.mfa_enabled.isnot(True),
            _signs_in_by_password(),
        )
        .all()
    )
    return sum(
        session_policy.mfa_covered(
            [name for name, on in (perms or {}).items() if on], sa is True, level
        )
        for sa, perms in users
    )


def without_key(session: Session, level: str) -> int:
    """Active accounts ``level`` of require_phishing_resistant covers that hold no
    security key: they can't finish signing in until they add one."""
    keyed = session.query(SecurityKey.principal_id).filter(
        SecurityKey.principal_id == User.principal_id
    )
    users = (
        session.query(User.service_account, Role.permissions)
        .outerjoin(Role, Role.role_id == User.role_id)
        .filter(User.is_active.is_(True), ~keyed.exists(), _signs_in_by_password())
        .all()
    )
    return sum(
        session_policy.mfa_covered(
            [name for name, on in (perms or {}).items() if on], sa is True, level
        )
        for sa, perms in users
    )


def _gap() -> int:
    """People without MFA the policy does not yet require it of; 0 at "everyone"."""
    with unit_of_work() as session:
        if session_policy.policy().require_mfa == "everyone":
            return 0
        return without_mfa(session)


def warning(user: User) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``sessions.manage``."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    count = _gap()
    if not count:
        return []
    return [
        {
            "message": f"{count} {'person' if count == 1 else 'people'} can sign in without MFA",
            "link": SETTINGS_LINK,
        }
    ]


def keys_unavailable(user: User) -> list[dict[str, str]]:
    """The bell's condition while keys are required but this install has none:
    covered people sign in with the app (marked as a step-down) until keys return."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    if session_policy.policy().require_phishing_resistant == "nobody":
        return []
    now = security_keys.availability()
    if now.available:
        return []
    return [
        {
            "message": f"Security keys are required but unavailable. {now.reason} "
            "Covered people sign in with the app until they return.",
            "link": SETTINGS_LINK,
        }
    ]


def register() -> None:
    notices.CONDITIONS.append(warning)
    notices.CONDITIONS.append(keys_unavailable)


def record_daily() -> bool:
    """Write today's count to the trail unless it holds one already; whether it wrote.

    Safe on several replicas and on restarts: the lock serializes the check and the
    write, and the check is for an entry since UTC midnight.
    """
    with unit_of_work() as session:
        if not trail_events.ready(session):
            return False
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
        policy = session_policy.policy()
        if policy.require_mfa == "everyone":
            return False
        done = session.execute(
            text(
                "SELECT 1 FROM audit_trail WHERE payload->>'action' = :a "
                "AND occurred_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'UTC' LIMIT 1"
            ),
            {"a": ACTION},
        ).first()
        if done:
            return False
        trail_events.record(
            session,
            ACTION,
            actor=ACTOR,
            count=without_mfa(session),
            require_mfa=policy.require_mfa,
        )
        return True
