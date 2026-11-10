"""How long a sign-in lasts: idle and absolute limits, in one validated model.

One ``system_config`` row holds the document: the fields of ``SessionPolicy``
plus its ``version``. A later setting joins by adding a field to the model.
Every ``limits`` call reads the row, so a saved change applies at the next
request; nothing is cached. Authentication and refresh read the limits only
through ``limits``.
"""

from datetime import datetime, timedelta
from typing import Iterable, NamedTuple, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work

KEY = "auth.session_policy"

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
        "sessions.manage",
    }
)


class SessionPolicy(BaseModel):
    idle_standard_minutes: int = Field(15, ge=1, le=60)
    idle_privileged_minutes: int = Field(10, ge=1, le=30)
    absolute_hours: int = Field(12, ge=1, le=24)
    # Most open sign-ins one account may hold; None: no limit.
    sign_in_limit: Optional[int] = Field(None, ge=1, le=10)

    @model_validator(mode="after")
    def _privileged_within_standard(self):
        if self.idle_privileged_minutes > self.idle_standard_minutes:
            raise ValueError(
                f"idle_privileged_minutes ({self.idle_privileged_minutes}) cannot "
                f"exceed idle_standard_minutes ({self.idle_standard_minutes})"
            )
        return self


def is_loosening(old: SessionPolicy, new: SessionPolicy) -> bool:
    """Whether ``new`` lets a sign-in last longer or an account hold more."""
    more_sign_ins = old.sign_in_limit is not None and (
        new.sign_in_limit is None or new.sign_in_limit > old.sign_in_limit
    )
    return (
        new.idle_standard_minutes > old.idle_standard_minutes
        or new.idle_privileged_minutes > old.idle_privileged_minutes
        or new.absolute_hours > old.absolute_hours
        or more_sign_ins
    )


class Stored(NamedTuple):
    policy: SessionPolicy
    # 0 until the first save: the defaults are version 0.
    version: int
    updated_by: Optional[str]
    updated_at: Optional[datetime]


def read_row(session: Session, lock: bool = False) -> tuple[Stored, bool]:
    """The stored document (defaults when no row) and whether a row exists."""
    row = (
        session.query(SystemConfig).filter(SystemConfig.key == KEY).populate_existing()
    )
    row = (row.with_for_update() if lock else row).first()
    if row is None:
        return Stored(SessionPolicy(), 0, None, None), False
    value = dict(row.value or {})
    version = int(value.pop("version", 0))
    return Stored(SessionPolicy(**value), version, row.updated_by, row.updated_at), True


class Limits(NamedTuple):
    # None: no idle limit (a service account's).
    idle: Optional[timedelta]
    absolute: timedelta
    # None: no limit on open sign-ins (a service account has none).
    sign_in_limit: Optional[int] = None


def _policy() -> SessionPolicy:
    with unit_of_work() as session:
        try:
            return read_row(session)[0].policy
        except (ValidationError, TypeError, ValueError):
            # A row edited by hand into something invalid: the defaults hold.
            return SessionPolicy()


def limits(permissions: Iterable[str], service_account: bool = False) -> Limits:
    """The limits for an account holding ``permissions``.

    A service account is bound by the absolute limit only: it has no person
    whose idleness could end it, and no sign-in limit.
    """
    policy = _policy()
    absolute = timedelta(hours=policy.absolute_hours)
    if service_account:
        return Limits(None, absolute)
    privileged = not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)
    minutes = (
        policy.idle_privileged_minutes if privileged else policy.idle_standard_minutes
    )
    return Limits(timedelta(minutes=minutes), absolute, policy.sign_in_limit)
