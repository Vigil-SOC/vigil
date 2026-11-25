"""How long a sign-in lasts, when failed sign-ins lock an account, when an idle one is deactivated and what a
password must be, in one validated model.

One ``system_config`` row holds the document: the fields of ``SessionPolicy``
plus its ``version``. A later setting joins by adding a field to the model.
Every ``limits`` call reads the row, so a saved change applies at the next
request; nothing is cached. Authentication and refresh read the limits only
through ``limits``; the password rules and lockout through ``policy``.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Iterable, Literal, NamedTuple, Optional, Union

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.auth.password_hash import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
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
    # 0: no idle limit. The two are independent; a privileged limit may be longer.
    idle_standard_minutes: int = Field(
        15,
        ge=0,
        le=720,
        description=(
            "Minutes with no activity before a standard sign-in ends. "
            "0 is no idle limit. 0 to 720 (12 hours)."
        ),
    )
    idle_privileged_minutes: int = Field(
        10,
        ge=0,
        le=720,
        description=(
            "Minutes with no activity before a privileged sign-in ends, "
            "independent of the standard limit. 0 is no idle limit. Also how "
            "long a Confirm-it's-you lasts, up to 12 hours; at 0, 10 minutes."
        ),
    )
    absolute_hours: int = Field(12, ge=1, le=24)
    # Most open sign-ins one account may hold; None: no limit.
    sign_in_limit: Optional[int] = Field(None, ge=1, le=10)
    # Characters (code points), from the shortest password up to the longest one
    # hashing will accept. A sign-in without MFA rechecks a password shorter
    # than the lower of 15 and this minimum.
    password_min_length: int = Field(16, ge=MIN_PASSWORD_LENGTH, le=MAX_PASSWORD_LENGTH)
    # zxcvbn score a new password must reach, 0–4. 0 requires no score.
    # Lowering it is a loosening. A save that leaves it out keeps the stored value.
    password_min_score: int = Field(3, ge=0, le=4)
    password_character_rules: Literal["none", "letters_digits", "four_classes"] = "none"
    # Previous passwords kept, and refused on reuse.
    password_history: int = Field(5, ge=4, le=24)
    # Days a password lasts before its person must change it; None: no expiry.
    # Any change to it is a person's act.
    password_max_age_days: Optional[int] = Field(None, ge=1, le=365)
    # Days after a change before a voluntary one is allowed; 0: off.
    password_min_age_days: int = Field(0, ge=0, le=7)
    # Failures in a row that lock an account. Off, the lock holds until an
    # administrator's Unlock or the person's reset link; on, it ends after
    # lockout_minutes. LOCK_UNTIL_UNLOCKED failures lock until unlocked either way.
    lockout_threshold: int = Field(3, ge=3, le=10)
    lockout_timed: bool = False
    lockout_minutes: int = Field(30, ge=30, le=1440)
    # Days without a sign-in or a personal MCP token's use before an account is
    # deactivated; None: never. Any change to it is a person's act.
    dormancy_days: Optional[int] = Field(35, ge=7, le=90)
    # Whose sign-in needs MFA: nobody, accounts holding a privileged permission,
    # or every person. A service account is never covered.
    require_mfa: Literal["off", "privileged", "everyone"] = "privileged"
    # Whose sign-in needs a security key: the app no longer counts for them. Never
    # above require_mfa. Raising it is a person's act, and needs the person to hold a key.
    require_phishing_resistant: Literal["nobody", "privileged", "everyone"] = "nobody"
    # The most days a token may live, by who holds it. Lowering one also shortens
    # the tokens already issued; raising one never lengthens them.
    personal_token_max_days: int = Field(365, ge=1, le=365)
    service_token_max_days: int = Field(365, ge=1, le=365)
    # Days a token may sit unused before it is revoked (reminded 7 days before);
    # None: never. A longer period, or off, is a loosening.
    token_unused_days: Optional[int] = Field(45, ge=7, le=365)

    @model_validator(mode="after")
    def _min_age_before_max_age(self):
        # Else the bell would send a person to a change the minimum age refuses.
        if (
            self.password_max_age_days is not None
            and self.password_min_age_days >= self.password_max_age_days
        ):
            raise ValueError(
                f"password_min_age_days ({self.password_min_age_days}) must be "
                f"less than password_max_age_days ({self.password_max_age_days})"
            )
        return self

    @model_validator(mode="after")
    def _keys_within_mfa(self):
        # Refused, never adjusted: either field may be the one that moved.
        if _rank(self.require_phishing_resistant) > _rank(self.require_mfa):
            raise ValueError(
                f"require_phishing_resistant ('{self.require_phishing_resistant}') "
                f"cannot be above require_mfa ('{self.require_mfa}')"
            )
        return self


# Failures in a row that lock until unlocked, even in timed mode.
LOCK_UNTIL_UNLOCKED = 100

_RULE_STRENGTH = {"none": 0, "letters_digits": 1, "four_classes": 2}


# Weakest first: a lower level is a loosening. "off" and "nobody" are the same rank.
MFA_LEVELS = ("off", "privileged", "everyone")
KEY_LEVELS = ("nobody", "privileged", "everyone")


def _rank(level: str) -> int:
    return (KEY_LEVELS if level in KEY_LEVELS else MFA_LEVELS).index(level)


def mfa_covered(
    permissions: Iterable[str], service_account: bool, level: str = "privileged"
) -> bool:
    """Whether an account holding ``permissions`` is covered at ``level``: of
    ``require_mfa``, or of ``require_phishing_resistant`` (needing a security key).

    The one coverage check: the sign-in gate, the counts and the warning all ask
    it. Privileged is judged by permissions held, never by role name.
    """
    if service_account or level in ("off", "nobody"):
        return False
    return level == "everyone" or not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)


def _longer_idle(old: int, new: int) -> bool:
    """Whether ``new`` lets a sign-in sit idle longer. 0 is no limit, the loosest."""
    if new == 0:
        return old != 0
    if old == 0:
        return False
    return new > old


def is_loosening(old: SessionPolicy, new: SessionPolicy) -> bool:
    """Whether ``new`` lets a sign-in last longer, an account hold more, a
    guesser have more tries or a shorter lock, an idle account stay longer, or a
    password be shorter, weaker, simpler, reused sooner, older or changed sooner,
    a token live longer, or an unused token stay valid longer. An idle of 0 is
    looser than any positive length; leaving 0, or lowering a nonzero idle, tightens."""
    weaker_rules = (
        _RULE_STRENGTH[new.password_character_rules]
        < _RULE_STRENGTH[old.password_character_rules]
    )
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
    longer_unused = old.token_unused_days is not None and (
        new.token_unused_days is None or new.token_unused_days > old.token_unused_days
    )
    lower_mfa = MFA_LEVELS.index(new.require_mfa) < MFA_LEVELS.index(old.require_mfa)
    lower_keys = _rank(new.require_phishing_resistant) < _rank(
        old.require_phishing_resistant
    )
    longer_max_age = old.password_max_age_days is not None and (
        new.password_max_age_days is None
        or new.password_max_age_days > old.password_max_age_days
    )
    return (
        lower_mfa
        or lower_keys
        or longer_max_age
        or new.password_min_age_days < old.password_min_age_days
        or _longer_idle(old.idle_standard_minutes, new.idle_standard_minutes)
        or _longer_idle(old.idle_privileged_minutes, new.idle_privileged_minutes)
        or new.absolute_hours > old.absolute_hours
        or more_sign_ins
        or new.password_min_length < old.password_min_length
        or new.password_min_score < old.password_min_score
        or weaker_rules
        or new.password_history < old.password_history
        or easier_lock
        or longer_dormancy
        or new.personal_token_max_days > old.personal_token_max_days
        or new.service_token_max_days > old.service_token_max_days
        or longer_unused
    )


def with_omitted_score(body: SessionPolicy, stored: SessionPolicy) -> SessionPolicy:
    """A save that leaves the strength score out keeps the stored score.

    Parsing has already filled the default, so an explicit value is told from
    an omission by whether the field was present. The loosening check, the dry
    run, the trail and the write all use this result.
    """
    if "password_min_score" in body.model_fields_set:
        return body
    if body.password_min_score == stored.password_min_score:
        return body
    return body.model_copy(update={"password_min_score": stored.password_min_score})


class Stored(NamedTuple):
    policy: SessionPolicy
    # 0 until the first save: the defaults are version 0.
    version: int
    updated_by: Optional[str]
    updated_at: Optional[datetime]
    # The row exists but cannot be parsed: ``policy`` is the defaults, at the
    # stored version, so a save overwrites it. Only set when ``read_row`` is
    # asked to tolerate it.
    unreadable: bool = False


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
        (
            "AUTH_MIN_PASSWORD_LENGTH",
            "password_min_length",
            12,
            MIN_PASSWORD_LENGTH,
            MAX_PASSWORD_LENGTH,
        ),
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


# Fixed key for the transaction advisory lock that orders a mint against a save,
# whether or not the row exists yet.
_LOCK_KEY = 47240001


def read_row(
    session: Session,
    lock: Union[bool, Literal["share"]] = False,
    tolerant: bool = False,
) -> tuple[Stored, bool]:
    """The stored document (defaults when no row) and whether a row exists.

    ``lock=True`` holds the policy until the transaction ends, for a save;
    ``"share"`` lets minters read together while a save waits for them. Both
    also take an advisory lock, so they order the same way when no row exists
    yet. An unparseable row raises ``ValidationError``, or with ``tolerant``
    reads as the defaults at the stored version.

    The first read with no row saves any password settings carried from the
    host environment as version 1 (#4175)."""
    if lock:
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock(:k)"
                if lock is True
                else "SELECT pg_advisory_xact_lock_shared(:k)"
            ),
            {"k": _LOCK_KEY},
        )
    for attempt in range(2):
        row = (
            session.query(SystemConfig)
            .filter(SystemConfig.key == KEY)
            .populate_existing()
        )
        if lock:
            row = row.with_for_update(read=lock == "share")
        row = row.first()
        if row is not None or attempt:
            break
        _seed(session)
    if row is None:
        return Stored(SessionPolicy(), 0, None, None), False
    try:
        value = dict(row.value or {})
        version = int(value.pop("version", 0))
        current, unreadable = SessionPolicy(**value), False
    except (ValidationError, TypeError, ValueError):
        if not tolerant:
            raise
        # Not a document at all, or a version that is not a number: the defaults
        # at version 0, which a save overwrites.
        version, current, unreadable = 0, SessionPolicy(), True
    return Stored(current, version, row.updated_by, row.updated_at, unreadable), True


class Limits(NamedTuple):
    # None: no idle limit (a service account, or an idle setting of 0).
    idle: Optional[timedelta]
    absolute: timedelta
    # None: no limit on open sign-ins (a service account has none).
    sign_in_limit: Optional[int] = None


def policy() -> SessionPolicy:
    """The policy in force now, read every call."""
    with unit_of_work() as session:
        try:
            return read_row(session)[0].policy
        except (ValidationError, TypeError, ValueError):
            # A row edited by hand into something invalid: the defaults hold.
            return SessionPolicy()


def limits(
    permissions: Iterable[str],
    service_account: bool = False,
    current: Optional[SessionPolicy] = None,
) -> Limits:
    """The limits for an account holding ``permissions``.

    A service account is bound by the absolute limit only: it has no person
    whose idleness could end it, and no sign-in limit. An idle setting of 0 is
    no idle limit for a person too: ``idle`` is None, not a zero timedelta.
    ``current`` is the policy when the caller has read it already.
    """
    current = current or policy()
    absolute = timedelta(hours=current.absolute_hours)
    if service_account:
        return Limits(None, absolute)
    privileged = not PRIVILEGED_PERMISSIONS.isdisjoint(permissions)
    minutes = (
        current.idle_privileged_minutes if privileged else current.idle_standard_minutes
    )
    idle = None if minutes == 0 else timedelta(minutes=minutes)
    return Limits(idle, absolute, current.sign_in_limit)
