"""How long a sign-in lasts, when failed sign-ins lock an account and when an idle one is deactivated, in one validated model.

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
    # Failures in a row that lock an account. Off, the lock holds until an
    # administrator's Unlock or the person's reset link; on, it ends after
    # lockout_minutes. LOCK_UNTIL_UNLOCKED failures lock until unlocked either way.
    lockout_threshold: int = Field(3, ge=1, le=20)
    lockout_timed: bool = False
    lockout_minutes: int = Field(30, ge=30, le=1440)
    # Days without a sign-in or a personal MCP token's use before an account is
    # deactivated; None: never. Any change to it is a person's act.
    dormancy_days: Optional[int] = Field(35, ge=7, le=90)

    @model_validator(mode="after")
    def _privileged_within_standard(self):
        if self.idle_privileged_minutes > self.idle_standard_minutes:
            raise ValueError(
                f"idle_privileged_minutes ({self.idle_privileged_minutes}) cannot "
                f"exceed idle_standard_minutes ({self.idle_standard_minutes})"
            )
        return self


# Failures in a row that lock until unlocked, even in timed mode.
LOCK_UNTIL_UNLOCKED = 100


def is_loosening(old: SessionPolicy, new: SessionPolicy) -> bool:
    """Whether ``new`` lets a sign-in last longer, an account hold more, or a
    guesser have more tries or a shorter lock, or an idle account stay longer."""
    easier_lock = (
        new.lockout_threshold > old.lockout_threshold
        or (new.lockout_timed and not old.lockout_timed)
        # A shorter timed lock is a weaker one.
        or (
            new.lockout_timed
            and old.lockout_timed
            and new.lockout_minutes < old.lockout_minutes
        )
    )
    more_sign_ins = old.sign_in_limit is not None and (
        new.sign_in_limit is None or new.sign_in_limit > old.sign_in_limit
    )
    longer_dormancy = old.dormancy_days is not None and (
        new.dormancy_days is None or new.dormancy_days > old.dormancy_days
    )
    return (
        new.idle_standard_minutes > old.idle_standard_minutes
        or new.idle_privileged_minutes > old.idle_privileged_minutes
        or new.absolute_hours > old.absolute_hours
        or more_sign_ins
        or easier_lock
        or longer_dormancy
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


def policy() -> SessionPolicy:
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
    current = policy()
    absolute = timedelta(hours=current.absolute_hours)
    if service_account:
        return Limits(None, absolute)
    privileged = not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)
    minutes = (
        current.idle_privileged_minutes if privileged else current.idle_standard_minutes
    )
    return Limits(timedelta(minutes=minutes), absolute, current.sign_in_limit)
