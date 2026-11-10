"""How long a sign-in lasts and what a password must be, in one validated model.

One ``system_config`` row holds the document: the fields of ``SessionPolicy``
plus its ``version``. A later setting joins by adding a field to the model.
Every ``limits`` call reads the row, so a saved change applies at the next
request; nothing is cached. Authentication and refresh read the limits only
through ``limits``; the password rules are read through ``current``.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Iterable, Literal, NamedTuple, Optional

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.config import _settings_env_file
from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

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
    # Characters (code points). 15 is the floor until Require MFA (#4177) lets
    # an install with MFA for everyone go down to 12.
    password_min_length: int = Field(16, ge=15, le=64)
    password_character_rules: Literal["none", "letters_digits", "four_classes"] = "none"
    # Previous passwords kept, and refused on reuse.
    password_history: int = Field(5, ge=4, le=24)

    @model_validator(mode="after")
    def _privileged_within_standard(self):
        if self.idle_privileged_minutes > self.idle_standard_minutes:
            raise ValueError(
                f"idle_privileged_minutes ({self.idle_privileged_minutes}) cannot "
                f"exceed idle_standard_minutes ({self.idle_standard_minutes})"
            )
        return self


_RULE_STRENGTH = {"none": 0, "letters_digits": 1, "four_classes": 2}


def is_loosening(old: SessionPolicy, new: SessionPolicy) -> bool:
    """Whether ``new`` lets a sign-in last longer, an account hold more, or a
    password be shorter, simpler or reused sooner."""
    weaker_rules = (
        _RULE_STRENGTH[new.password_character_rules]
        < _RULE_STRENGTH[old.password_character_rules]
    )
    more_sign_ins = old.sign_in_limit is not None and (
        new.sign_in_limit is None or new.sign_in_limit > old.sign_in_limit
    )
    return (
        new.idle_standard_minutes > old.idle_standard_minutes
        or new.idle_privileged_minutes > old.idle_privileged_minutes
        or new.absolute_hours > old.absolute_hours
        or more_sign_ins
        or new.password_min_length < old.password_min_length
        or weaker_rules
        or new.password_history < old.password_history
    )


class Stored(NamedTuple):
    policy: SessionPolicy
    # 0 until the first save: the defaults are version 0.
    version: int
    updated_by: Optional[str]
    updated_at: Optional[datetime]


CARRIED = "carried from host configuration"


def _carried_values() -> dict:
    """Password settings an operator chose in the host environment before they
    moved into this document. A value equal to the old default counts as unset.
    Read only while no row exists: once one is saved the variables are never
    read again."""
    env_file = _settings_env_file()
    env = dict(dotenv_values(env_file)) if env_file else {}
    env.update(os.environ)
    carried = {}
    for var, field, old_default, low, high in (
        ("AUTH_MIN_PASSWORD_LENGTH", "password_min_length", 12, 15, 64),
        ("AUTH_PASSWORD_HISTORY_LIMIT", "password_history", 5, 4, 24),
    ):
        try:
            value = int(env.get(var) or old_default)
        except ValueError:
            continue
        if value != old_default:
            carried[field] = max(low, min(high, value))  # moved into range
    return carried


def _seed(session: Session) -> None:
    """Save any carried host values as version 1, once."""
    carried = _carried_values()
    if not carried:
        return
    value = {**SessionPolicy(**carried).model_dump(), "version": 1}
    inserted = session.execute(
        insert(SystemConfig)
        .values(
            key=KEY,
            value=value,
            description="Session policy: sign-in limits and password rules",
            config_type="auth",
            updated_by=CARRIED,
        )
        .on_conflict_do_nothing()
    )
    if inserted.rowcount:
        logger.warning("Session policy carried from host configuration: %s", carried)
        trail_events.record(
            session, "session_policy.carried", actor=CARRIED, version=1, new=value
        )


def read_row(session: Session, lock: bool = False) -> tuple[Stored, bool]:
    """The stored document (defaults when no row) and whether a row exists.

    The first read with no row saves any password settings carried from the
    host environment as version 1 (#4175)."""
    for attempt in range(2):
        row = (
            session.query(SystemConfig)
            .filter(SystemConfig.key == KEY)
            .populate_existing()
        )
        row = (row.with_for_update() if lock else row).first()
        if row is not None or attempt:
            break
        _seed(session)
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


def current() -> SessionPolicy:
    """The policy in force now, read every call."""
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
    policy = current()
    absolute = timedelta(hours=policy.absolute_hours)
    if service_account:
        return Limits(None, absolute)
    privileged = not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)
    minutes = (
        policy.idle_privileged_minutes if privileged else policy.idle_standard_minutes
    )
    return Limits(timedelta(minutes=minutes), absolute, policy.sign_in_limit)
