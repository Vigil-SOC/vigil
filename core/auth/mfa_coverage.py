"""People who can sign in without MFA while the policy does not require it of everyone (#4177).

The count uses the policy's own coverage check at the "everyone" level. It feeds
a live warning for those who manage sessions and a daily entry on the audit trail.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import (
    security_keys,
    session_policy,
    sign_ins,
    sso,
    sso_settings,
    trail_events,
)
from core.platform import notices
from core.storage.models import Role, SecurityKey, SignIn, User
from core.storage.unit_of_work import unit_of_work

ACTION = "auth.mfa_coverage_gap"
ACTOR = "system"
# Advisory lock key: replicas take turns at the once-a-day check.
LOCK = 4177
SETTINGS_LINK = "/settings?section=sessions"


def _latest_sso(session: Session, principal_ids: list[str]) -> dict[str, Optional[str]]:
    """Each principal's latest SSO sign-in's ``mfa_met``, in one query."""
    if not principal_ids:
        return {}
    rows = (
        session.query(SignIn.principal_id, SignIn.mfa_met)
        .filter(
            SignIn.principal_id.in_(principal_ids),
            SignIn.auth_method == sign_ins.SSO,
        )
        .distinct(SignIn.principal_id)
        .order_by(SignIn.principal_id, SignIn.started_at.desc())
        .all()
    )
    return dict(rows)


def _count(session: Session, level: str, held: tuple[str, ...], *filters) -> int:
    """Active accounts ``level`` covers that match ``filters``, less those who sign
    in only through the provider and whose latest SSO sign-in met it with ``held``.
    No SSO sign-in, or one from before the check, holds nothing (#4758)."""
    users = [
        user
        for user, perms in session.query(User, Role.permissions)
        .outerjoin(Role, Role.role_id == User.role_id)
        .filter(User.is_active.is_(True), *filters)
        .all()
        if session_policy.mfa_covered(
            [name for name, on in (perms or {}).items() if on],
            user.service_account is True,
            level,
        )
    ]
    if not users:
        return 0
    current = sso_settings.settings()
    only = [u.principal_id for u in users if sso.sso_only(session, u, current)]
    latest = _latest_sso(session, only)
    only = set(only)
    return sum(
        u.principal_id not in only or latest.get(u.principal_id) not in held
        for u in users
    )


def without_mfa(session: Session, level: str = "everyone") -> int:
    """Active accounts ``level`` would cover that have no MFA yet. A step-down
    counts: it is how the provider's mfa is recorded while keys are unavailable."""
    return _count(
        session,
        level,
        (sign_ins.PROVIDER_MFA, sign_ins.PROVIDER_PHR, sign_ins.STEP_DOWN),
        User.mfa_enabled.isnot(True),
    )


def without_key(session: Session, level: str) -> int:
    """Active accounts ``level`` of require_phishing_resistant covers that hold no
    security key: they can't finish signing in until they add one."""
    keyed = session.query(SecurityKey.principal_id).filter(
        SecurityKey.principal_id == User.principal_id
    )
    return _count(session, level, (sign_ins.PROVIDER_PHR,), ~keyed.exists())


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
