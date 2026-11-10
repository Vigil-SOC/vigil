"""How long a sign-in lasts: idle and absolute limits, in one validated model.

These are defaults for now; the policy row that makes them editable replaces
``_policy()`` and nothing else. Authentication and refresh read the limits only
through ``limits``.
"""

from datetime import timedelta
from typing import Iterable, NamedTuple, Optional

from pydantic import BaseModel, Field

# A role holding any of these is privileged: judged by permissions held, never
# by the role's name.
PRIVILEGED_PERMISSIONS = frozenset(
    {
        "users.write",
        "users.delete",
        "roles.manage",
        "audit.forward",
        "audit.manage",
        "integrations.write",
        "integrations.install",
        "ai_autonomy.write",
        "ai_decisions.approve",
    }
)


class SessionPolicy(BaseModel):
    idle_standard_minutes: int = Field(15, ge=5, le=480)
    idle_privileged_minutes: int = Field(10, ge=5, le=480)
    absolute_hours: int = Field(12, ge=1, le=72)


class Limits(NamedTuple):
    # None: no idle limit (a service account's).
    idle: Optional[timedelta]
    absolute: timedelta


def _policy() -> SessionPolicy:
    return SessionPolicy()


def limits(permissions: Iterable[str], service_account: bool = False) -> Limits:
    """The limits for an account holding ``permissions``.

    A service account is bound by the absolute limit only: it has no person
    whose idleness could end it.
    """
    policy = _policy()
    absolute = timedelta(hours=policy.absolute_hours)
    if service_account:
        return Limits(None, absolute)
    privileged = not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)
    minutes = (
        policy.idle_privileged_minutes if privileged else policy.idle_standard_minutes
    )
    return Limits(timedelta(minutes=minutes), absolute)
