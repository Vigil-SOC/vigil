"""The sweep that warns, then deactivates, accounts idle past the policy's dormancy period (#4176).

Runs about hourly on each replica. The policy is read afresh on every sweep; with
dormancy off it does nothing. Each due account is handled under a row lock and
re-checked there, so two replicas, or a restart, neither repeat a warning nor
deactivate twice. ``dormancy_warned_at`` records the warning, one per idle stretch,
and an account is deactivated only once a warning has been out for the warning
window, so a shortened period warns first. Notices go out after the commit.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import dormancy, principals, session_policy, trail_events
from core.auth.auth_service import managers
from core.auth.deactivation import deactivate_account, tell_managers, when_committed
from core.auth.sign_in_runs import live_runs
from core.platform.notices import notify
from core.storage.models import Principal, SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 3600
# Startup does not wait on the schema; the first sweep comes after it settles.
FIRST_SWEEP_SECONDS = 60
ACTOR = "system"
# Advisory lock key serializing the sweep's per-account decisions across replicas.
LOCK = 4176


async def run() -> None:
    await asyncio.sleep(FIRST_SWEEP_SECONDS)
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001 - the next sweep tries again
            logger.exception("dormancy sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


def sweep() -> None:
    days = session_policy.policy().dormancy_days
    if days is None:
        return
    now = utcnow()
    with unit_of_work() as session:
        last = dormancy.activity(session)
        # Active people only: a service account is never deactivated for idleness.
        people = (
            session.query(User.user_id)
            .join(Principal, Principal.principal_id == User.principal_id)
            .filter(
                User.is_active.is_(True),
                User.service_account.isnot(True),
                Principal.kind == principals.PERSON,
                Principal.deleted_at.is_(None),
            )
            .all()
        )
    for (user_id,) in people:
        if user_id not in last or now < dormancy.warning_start(last[user_id], days):
            continue
        try:
            _handle(user_id, days, now)
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            logger.exception("dormancy sweep could not handle an account")


def _handle(user_id: str, days: int, now) -> None:
    # Imported here: the router imports this package's clock, not the other way.
    from services.api.routers.users import _leaves_no_manager

    with unit_of_work() as session:
        # One at a time across replicas, so two idle role managers cannot each
        # be deactivated on the strength of the other still being active.
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
        user = (
            session.query(User)
            .filter(User.user_id == user_id)
            .with_for_update()
            .first()
        )
        if user is None or not user.is_active or user.service_account is True:
            return
        last = dormancy.activity(session, [user_id])[user_id]
        start = dormancy.warning_start(last, days)
        warned = user.dormancy_warned_at
        if warned is None or warned < start:
            # No warning yet in this idle stretch: it comes first, and the
            # deactivation after the window, even if the period was just shortened.
            user.dormancy_warned_at = now
            due = max(dormancy.deadline(last, days), now + dormancy.window(days))
            _warn(session, user, due, last)
            return
        if now < max(dormancy.deadline(last, days), warned + dormancy.window(days)):
            return
        sole = _leaves_no_manager(session, users={user_id: (user.role_id, False)})
        if sole:
            # The last person who can manage roles stays; managers are told once.
            if user.dormancy_held_at is None or user.dormancy_held_at < start:
                user.dormancy_held_at = now
                _tell_held(session, user, last, days)
            return
        deactivate_account(session, user, actor=ACTOR, by_person=False)
        trail_events.record(
            session,
            "user.deactivated_dormant",
            actor=ACTOR,
            principal=user.principal_id,
            last_activity=last.isoformat(),
            dormancy_days=days,
        )


def _day(when) -> str:
    return when.strftime("%Y-%m-%d") + " UTC"


def _warn(session: Session, user: User, due, last) -> None:
    """To the holder and the managers once committed: when, and which agent runs
    deactivating would stop."""
    sids = [
        sid
        for (sid,) in session.query(SignIn.sign_in_id).filter(
            SignIn.principal_id == user.principal_id
        )
    ]
    runs = live_runs(sids) if sids else {}
    listing = (
        "\n".join(f"- {label} ({run_id})" for run_id, label in runs.items())
        if runs
        else "none"
    )
    message = (
        f"{user.username} has had no sign-in or personal MCP token use since "
        f"{_day(last)}, so the account will be deactivated on {_day(due)}. "
        "A sign-in or a personal MCP token's use before then keeps it. "
        "Deactivating revokes its MCP credentials and stops the agent runs "
        f"started from its sign-ins. Agent runs in flight now:\n{listing}"
    )
    title = f"{user.username} will be deactivated for inactivity"
    principal, name = user.principal_id, user.username

    def send() -> None:
        for to in [principal, *managers(principal)]:
            try:
                notify(to, "dormancy_warning", title, message, link="/settings")
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a dormancy warning for %s", name)

    when_committed(session, send)


def _tell_held(session: Session, user: User, last, days: int) -> None:
    message = (
        f"{user.username} has been idle since {_day(last)}, past the {days}-day "
        "dormancy period, but is the last active person who can manage roles, "
        "so the account was not deactivated. Give another person that "
        "permission, or deactivate it yourself."
    )
    tell_managers(
        session,
        user,
        "dormancy_held",
        f"{user.username} is idle and was kept: the last role manager",
        message,
        link="/settings",
        priority="high",
    )
