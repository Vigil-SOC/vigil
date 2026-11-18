"""The unused-token rule: a token idle past ``token_unused_days`` is reminded, then revoked (#4725).

A token's clock is its last use, or its creation if it was never used. The period
ends ``token_unused_days`` after that. A reminder goes out no earlier than 7 days
before the end and *counts* when it was sent after the clock, after the last restore
settled, and no earlier than 7 days before the end of the period now in force. With
no counting reminder the sweep sends one; with one at least 7 days old and the period
over, the token is revoked, as a system act. The sweep (``services/api/token_sweep``)
and ``authenticate`` both revoke through :func:`revoke_unused`, so there is exactly one
trail entry and one notice whichever gets there first. Refusal at use reads stored
state only. A policy that cannot be read does nothing: no reminder, no revocation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.auth import (
    dormancy,
    principals,
    session_policy,
    token_notices,
    token_policy,
    trail_events,
)
from core.platform import notices
from core.platform.notices import send_notices
from core.storage.models import McpCredential, SystemConfig, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# The reminder comes this long before the period ends, and a revocation follows it
# by at least this long.
WARN = timedelta(days=dormancy.WARN_DAYS)

# When the last whole pass finished: one row, shaped like dormancy.GRACE_KEY.
PASS_KEY = "auth.token_pass_completed_at"
# The bell speaks once the pass has been late this long.
LATE = timedelta(hours=3)
# Without a stamp, lateness counts from here, so an upgrade does not raise it at once.
_STARTED = utcnow()
LATE_MESSAGE = (
    "The unused-token pass has not completed in over 3 hours, so API tokens left "
    "unused are not being reminded or revoked. The API service has to be running "
    "for it: its logs show why the pass fails, and a restart runs it again."
)


class State(NamedTuple):
    days: int
    # When the last restore settled; a reminder from before it does not count.
    restored: Optional[datetime]
    # Directory sync is on: its token is exempt.
    sync_on: bool


def read_state(session: Session, locked: bool = False) -> Optional[State]:
    """The rule in force, or None when it is off or the policy cannot be read (never
    the defaults). ``locked`` reads the policy FOR SHARE, as a mint does."""
    try:
        policy = (
            token_policy.current(session)
            if locked
            else session_policy.read_row(session)[0].policy
        )
    except (token_policy.PolicyUnreadable, ValidationError, TypeError, ValueError):
        return None
    if policy.token_unused_days is None:
        return None
    from core.auth import sso_settings

    try:
        sync_on = sso_settings.read_row(session)[0].settings.sync_enabled
    except (ValidationError, TypeError, ValueError):
        sync_on = True  # unreadable: leave the sync token alone
    try:
        restored = dormancy.settled_at(session)
    except (KeyError, TypeError, ValueError):
        return None  # a restore marker that can't be read: do nothing
    return State(policy.token_unused_days, restored, sync_on)


def clock(credential: McpCredential) -> datetime:
    return credential.last_used_at or credential.created_at


def period_end(credential: McpCredential, days: int) -> datetime:
    return clock(credential) + timedelta(days=days)


def counts(credential: McpCredential, state: State) -> bool:
    """Whether the token's reminder is one that counts."""
    if credential.reminded_at is None:
        return False
    floor = max(clock(credential), period_end(credential, state.days) - WARN)
    if state.restored is not None:
        floor = max(floor, state.restored)
    return credential.reminded_at >= floor


def decide(
    credential: McpCredential, holder: User, state: State, now: datetime
) -> Optional[str]:
    """``"remind"``, ``"revoke"`` or None, for an unrevoked, unexpired token."""
    from core.auth.directory_sync import is_sync_account

    if (
        credential.revoked_at is not None
        or (credential.expires_at is not None and credential.expires_at <= now)
        # A rotated token's successor carries the clock.
        or credential.successor_id is not None
        or (state.sync_on and is_sync_account(holder))
    ):
        return None
    end = period_end(credential, state.days)
    if now < end - WARN:
        return None
    if not counts(credential, state):
        return "remind"
    if now >= end and now >= credential.reminded_at + WARN:
        return "revoke"
    return None


def refused(
    credential: McpCredential, holder: User, session: Session, now: datetime
) -> bool:
    """Whether use of the token is refused now: its period is over and a counting
    reminder went out at least 7 days ago. Reads only for a reminder that old."""
    if credential.reminded_at is None or now < credential.reminded_at + WARN:
        return False
    state = read_state(session)
    return state is not None and decide(credential, holder, state, now) == "revoke"


def revoke_unused(credential_id: str, now: Optional[datetime] = None) -> bool:
    """Revoke the token if it is still due under the row lock. True if this call did:
    only then the trail entry and the notice are made, so there is one of each."""
    from core.auth import mcp_credential_service

    now = now or utcnow()
    with unit_of_work() as session:
        # Policy first, then the row, as a rotation and a policy save order them.
        state = read_state(session, locked=True)
        if state is None:
            return False
        credential = (
            session.query(McpCredential)
            .filter(McpCredential.credential_id == credential_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        holder = session.get(User, credential.user_id) if credential else None
        if (
            holder is None
            or decide(credential, holder, state, now) != "revoke"
            or not mcp_credential_service.revoke(credential_id, session)
        ):
            return False
        trail_events.record(
            session,
            "credential.revoked",
            actor=principals.SYSTEM_PRINCIPAL_ID,
            principal=holder.principal_id,
            credential_id=credential_id,
            reason=f"unused {state.days} days",
        )
        # A failed notice does not undo the revocation (tell swallows it).
        notices = token_notices.tell(
            holder,
            "API token revoked: unused",
            token_notices.describe(credential, holder)
            + f" was revoked because it was not used for {state.days} days.",
            "high",
            session=session,
        )
    send_notices(notices)
    return True


def refuse(credential_id: str) -> None:
    """Revoke a token ``authenticate`` has refused for disuse. The refusal stands if
    this fails: the next use tries again."""
    try:
        revoke_unused(credential_id)
    except Exception:  # noqa: BLE001 - refused regardless
        logger.exception("could not revoke unused token %s", credential_id)


def remind(credential_id: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Record the reminder if the token is still due one under the row lock. Returns
    the date it will be revoked unless used, or None if no reminder was sent here."""
    now = now or utcnow()
    with unit_of_work() as session:
        state = read_state(session, locked=True)
        if state is None:
            return None
        credential = (
            session.query(McpCredential)
            .filter(McpCredential.credential_id == credential_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        holder = session.get(User, credential.user_id) if credential else None
        if holder is None or decide(credential, holder, state, now) != "remind":
            return None
        credential.reminded_at = now
        return max(period_end(credential, state.days), now + WARN)


def first_reminder(
    credential: McpCredential, state: State, now: Optional[datetime] = None
) -> datetime:
    """When the token's reminder goes out, or went out: the counting one's date; else
    7 days before the period ends, or now if that has passed (the next pass sends it).
    A restore only voids earlier reminders, so it never pushes this past now."""
    if counts(credential, state):
        return credential.reminded_at
    return max(period_end(credential, state.days) - WARN, now or utcnow())


def stamp_pass() -> datetime:
    """Record that a whole pass just finished."""
    now = utcnow()
    value = {"completed_at": now.isoformat()}
    with unit_of_work() as session:
        session.execute(
            insert(SystemConfig)
            .values(
                key=PASS_KEY,
                value=value,
                description="When the last unused-token pass completed",
                config_type="auth",
                updated_by="token_sweep",
            )
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
    return now


def pass_health(session: Session) -> tuple[Optional[datetime], bool]:
    """When the last pass completed (None if never), and whether it is late."""
    value = (
        session.query(SystemConfig.value).filter(SystemConfig.key == PASS_KEY).scalar()
    )
    try:
        stamp = datetime.fromisoformat(value["completed_at"]) if value else None
    except (KeyError, TypeError, ValueError):
        stamp = None  # unreadable reads as never
    return stamp, utcnow() - (stamp or _STARTED) > LATE


def late(user: User) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``sessions.manage``: the pass is late.
    No setting turns it off; the next completed pass clears it."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    with unit_of_work() as session:
        if pass_health(session)[1]:
            return [{"message": LATE_MESSAGE, "link": "/settings?section=sessions"}]
    return []


def register() -> None:
    notices.CONDITIONS.append(late)
