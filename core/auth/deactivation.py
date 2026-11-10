"""Deactivating an account, the one way (#4176).

An account stops being able to act: it is marked inactive, its MCP credentials are
revoked and its sign-ins end (which stops their agent runs). Removal, a disable
and dormancy all come here; each records its own trail entry for why.
"""

from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import quote

from sqlalchemy import event, func
from sqlalchemy.orm import Session

from core.auth import mcp_credential_service, sign_ins, trail_events
from core.platform.notices import notify
from core.storage.models import Case, CaseTask, User

logger = logging.getLogger(__name__)

OPEN_TASKS = ("pending", "in_progress")


def when_committed(session: Session, send: Callable[[], None]) -> None:
    """Run ``send`` once ``session``'s outermost transaction has committed, so a
    rolled-back change tells nobody anything."""

    sent = []

    def run(committed: Session) -> None:
        # A SAVEPOINT release fires this too; the listener is kept (a session's
        # listeners cannot change while it dispatches), so it guards itself.
        if sent or committed.in_nested_transaction():
            return
        sent.append(True)
        try:
            send()
        except Exception:  # noqa: BLE001 - the commit stands
            logger.exception("a notice queued for a commit was not sent")

    event.listen(session, "after_commit", run)


def deactivate_account(
    session: Session,
    user: User,
    *,
    actor: str,
    by_person: bool,
    record_trail: bool = True,
) -> list[str]:
    """Deactivate ``user`` in ``session``; returns the credential ids revoked.

    ``by_person`` is whether a person at a screen did it. When not, the managers
    are told if the account owned open work. ``record_trail=False`` leaves the
    ``credential.revoked`` entries to the caller, for a disable that commits on
    its own ahead of the rest of a request.
    """
    user.is_active = False
    # Row before trail lock, the order every other path takes them in.
    session.flush()
    revoked = []
    for credential in mcp_credential_service.list_for_user(
        user.user_id, session=session
    ):
        if mcp_credential_service.revoke(credential.credential_id, session=session):
            revoked.append(credential.credential_id)
            if record_trail:
                trail_events.record(
                    session,
                    "credential.revoked",
                    actor=actor,
                    principal=user.principal_id,
                    credential_id=credential.credential_id,
                )
    sign_ins.end_all_for_user(user.user_id, sign_ins.DEACTIVATED, session=session)
    session.flush()
    if not by_person:
        _tell_managers_of_work(session, user)
    return revoked


def _tell_managers_of_work(session: Session, user: User) -> None:
    """One notice if the account owns open cases or tasks: counts only, no titles,
    the cases linked to the Cases list filtered to this owner. Sent on commit."""
    cases = (
        session.query(func.count())
        .select_from(Case)
        .filter(Case.assignee == user.username, Case.status != "closed")
        .scalar()
    )
    tasks = (
        session.query(func.count())
        .select_from(CaseTask)
        .filter(CaseTask.assignee == user.username, CaseTask.status.in_(OPEN_TASKS))
        .scalar()
    )
    if not (cases or tasks):
        return
    parts = []
    if cases:
        parts.append(f"{cases} open case{'s' if cases != 1 else ''}")
    if tasks:
        parts.append(f"{tasks} open task{'s' if tasks != 1 else ''}")
    message = (
        f"{user.username} was deactivated and owns {' and '.join(parts)}. "
        "Reassign the work."
    )
    link = f"/cases?owner={quote(user.username, safe='')}" if cases else None
    title = f"{user.username} was deactivated and owns open work"
    tell_managers(
        session,
        user,
        "deactivated_work",
        title,
        message,
        link=link,
        priority="high",
    )


def tell_managers(
    session: Session, user: User, kind: str, title: str, message: str, **notice
) -> None:
    """A notice about ``user`` to each manager, once ``session`` commits."""
    # Read now: the row is expired once the commit has happened.
    about = user.principal_id

    def send() -> None:
        from core.auth.auth_service import managers

        for principal in managers(about):
            try:
                notify(principal, kind, title, message, **notice)
            except Exception:  # noqa: BLE001 - the others are still told
                logger.exception("could not send a %s notice to %s", kind, principal)

    when_committed(session, send)
