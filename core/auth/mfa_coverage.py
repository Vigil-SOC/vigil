"""People who can sign in without MFA while the policy does not require it of everyone (#4177).

The count uses the policy's own coverage check at the "everyone" level. It feeds
a live warning for those who manage sessions and a daily entry on the audit trail.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import session_policy, trail_events
from core.platform import notices
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work

ACTION = "auth.mfa_coverage_gap"
ACTOR = "system"
# Advisory lock key: replicas take turns at the once-a-day check.
LOCK = 4177
SETTINGS_LINK = "/settings?section=sessions"


def without_mfa(session: Session) -> int:
    """Active accounts the "everyone" level would cover that have no MFA yet."""
    users = (
        session.query(User.service_account)
        .filter(User.is_active.is_(True), User.mfa_enabled.isnot(True))
        .all()
    )
    return sum(
        session_policy.mfa_covered((), sa is True, "everyone") for (sa,) in users
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


def register() -> None:
    notices.CONDITIONS.append(warning)


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
