"""A person's sign-in methods, listed from the ``users`` row (#4233, epic #4164
decision 17). No model or table: the authenticator app and the recovery codes
live on the row; security keys (#4234) are rows of their own table.

Never the secret or a code: a type, a name, when it was added and when it was
last used, and the recovery codes left.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import security_keys, trail_events
from core.storage.models import User
from core.storage.schemas.base import OptDateTime

# An authenticator step is this many seconds (RFC 6238's default).
TOTP_STEP_SECONDS = 30
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class MfaMethod(BaseModel):
    type: str  # "authenticator_app" | "security_key" | "recovery_codes"
    name: str
    # A security key's id, for removing it.
    key_id: Optional[str] = None
    added_at: OptDateTime = None
    last_used_at: OptDateTime = None
    # Recovery codes: how many are left.
    remaining: Optional[int] = None


def latest(session: Session, principal_id: str, *actions: str) -> Optional[datetime]:
    """When the newest trail entry of any of these actions about the person was
    written, or None (nothing yet, or no trail on this database)."""
    if not trail_events.ready(session):
        return None
    return session.execute(
        text(
            "SELECT max(occurred_at) FROM audit_trail "
            "WHERE payload->>'action' = ANY(:actions) "
            "AND payload->>'principal' = :principal"
        ),
        {"actions": list(actions), "principal": principal_id},
    ).scalar()


def enabled_before(session: Session, principal_id: str) -> bool:
    """Whether the person has turned MFA on or off before: the trail's answer to
    "turning it on again"."""
    return (
        latest(session, principal_id, "auth.mfa_enabled", "auth.mfa_disabled")
        is not None
    )


def _naive(moment: Optional[datetime]) -> Optional[datetime]:
    """Times are naive UTC in every list."""
    if moment is not None and moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def has_app(user: User, keyed: bool) -> bool:
    """Whether the person's authenticator app is confirmed. ``mfa_enabled`` means
    "has a method", so for a person with keys a secret whose first code never
    came (no TOTP step yet) is a setup still pending, not a method. So it is for
    a break-glass account, which holds MFA through the safe's codes."""
    held = keyed or user.break_glass_at is not None
    return bool(
        user.mfa_secret
        and user.mfa_enabled
        and (user.mfa_last_totp_step is not None or not held)
    )


def method_types(user: User, keyed: bool) -> list[str]:
    """The method types ``user`` has, in the order ``methods()`` lists them.

    Each type at most once. No key name, time or codes-left count: anyone who
    can read the people list sees this. Empty without MFA. ``keyed`` is whether
    the person has a security key.
    """
    if not user.mfa_enabled:
        return []
    found: list[str] = []
    if has_app(user, keyed):
        found.append("authenticator_app")
    if keyed:
        found.append("security_key")
    if user.mfa_recovery_codes:
        found.append("recovery_codes")
    return found


def methods(session: Session, user: User) -> list[dict]:
    """The methods ``user`` has enrolled. Empty without MFA."""
    # No key lookup until MFA is on: the type decision is the same guard.
    keys = security_keys.keys_of(session, user.principal_id) if user.mfa_enabled else []
    wanted = set(method_types(user, bool(keys)))
    principal = user.principal_id
    found: list[MfaMethod] = []
    if "security_key" in wanted:
        found.extend(
            MfaMethod(
                type="security_key",
                name=key.name,
                key_id=key.key_id,
                added_at=_naive(key.created_at),
                last_used_at=_naive(key.last_used_at),
            )
            for key in keys
        )
    if "authenticator_app" in wanted:
        step = user.mfa_last_totp_step
        used = None
        if step is not None:
            used = EPOCH + timedelta(seconds=step * TOTP_STEP_SECONDS)
        found.insert(
            0,
            MfaMethod(
                type="authenticator_app",
                name="Authenticator app",
                added_at=_naive(latest(session, principal, "auth.mfa_enabled")),
                last_used_at=_naive(used),
            ),
        )
    if "recovery_codes" in wanted:
        found.append(
            MfaMethod(
                type="recovery_codes",
                name="Recovery codes",
                added_at=_naive(
                    latest(
                        session,
                        principal,
                        "auth.mfa_enabled",
                        "auth.recovery_codes_regenerated",
                    )
                ),
                remaining=len(user.mfa_recovery_codes or []),
            )
        )
    return [m.model_dump() for m in found]
