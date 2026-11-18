"""The sweep that reminds, then revokes, API tokens left unused past the session policy's period (#4725).

Runs about hourly on each replica, whatever the dormancy setting and the restore
grace. The rule is core/auth/token_unused.py. One replica sweeps at a time (a
transaction advisory lock held for the pass), and each token is re-checked under its
row lock, so two replicas, or a restart, neither remind nor revoke twice. A recipient
gets one reminder notice per pass listing its tokens. A policy that can't be read
does nothing. Each pass also purges expired OAuth requests and clients unused for
30 days (``oauth_server.purge``). Each completed pass stamps its time (``token_unused.stamp_pass``), which
the Sessions section shows and the bell watches; a pass that found the lock held, or
raised, stamps nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import text

from core.auth import oauth_server, token_notices, token_unused
from core.platform.notices import send_notices
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 3600
# Startup does not wait on the schema; the first sweep comes after it settles.
FIRST_SWEEP_SECONDS = 60
# Advisory lock key serializing the sweep across replicas.
LOCK = 4725


async def run() -> None:
    await asyncio.sleep(FIRST_SWEEP_SECONDS)
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001 - the next sweep tries again
            logger.exception("token sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


def sweep() -> None:
    now = utcnow()
    # Held until this transaction ends, which is after the whole pass.
    with unit_of_work() as lock:
        if not lock.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK}
        ).scalar():
            return  # another replica is on it: not a completed pass here
        _pass(now)
        try:
            oauth_server.purge(now)
        except (
            Exception
        ):  # noqa: BLE001 - tokens were swept; the purge retries next pass
            logger.exception("OAuth purge failed")
        # Also when the rule is off or the policy unreadable; a pass that raised never gets here.
        token_unused.stamp_pass()


def _pass(now: datetime) -> None:
    with unit_of_work() as session:
        state = token_unused.read_state(session)
        if state is None:
            return
        due = [
            (
                credential.credential_id,
                token_unused.decide(credential, holder, state, now),
            )
            for credential, holder in session.query(McpCredential, User)
            .join(User, User.user_id == McpCredential.user_id)
            .filter(
                McpCredential.revoked_at.is_(None),
                McpCredential.successor_id.is_(None),
            )
            .all()
        ]
    reminded = {}
    for credential_id, action in due:
        try:
            if action == "revoke":
                token_unused.revoke_unused(credential_id, now)
            elif action == "remind":
                ends = token_unused.remind(credential_id, now)
                if ends:
                    reminded[credential_id] = ends
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            logger.exception("token sweep could not handle a token")
    if reminded:
        _tell(reminded)


def _tell(reminded: dict) -> None:
    """One notice per recipient for the tokens reminded this pass (each with its
    revocation date). The reminders are recorded already: a failed notice is not retried.
    """
    try:
        with unit_of_work() as session:
            rows = [
                (credential, holder, reminded[credential.credential_id])
                for credential, holder in session.query(McpCredential, User)
                .join(User, User.user_id == McpCredential.user_id)
                .filter(McpCredential.credential_id.in_(list(reminded)))
                .all()
            ]
            notices = token_notices.tell_reminded(rows, session)
        send_notices(notices)
    except (
        Exception
    ):  # noqa: BLE001 - the reminders stand; the notice is not their record
        logger.exception("token sweep could not send its reminders")
