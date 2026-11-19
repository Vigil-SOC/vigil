"""Deactivating an account, the one way (#4176).

An account stops being able to act: it is marked inactive, its MCP credentials are
revoked and its sign-ins end (which stops their agent runs). Removal, a disable
and dormancy all come here; each records its own trail entry for why.
"""

from __future__ import annotations

import logging
from typing import Callable, Union
from urllib.parse import quote

from sqlalchemy import event, or_
from sqlalchemy.orm import Session

from core.auth import invites, mcp_credential_service, sign_ins, trail_events
from core.auth.permissions import case_reader_for, may_read_case
from core.cases.assignee import TASK_DONE
from core.platform.notices import notify
from core.storage.models import Case, CaseTask, User
from core.time import utcnow

logger = logging.getLogger(__name__)

# Open is anything not done, so a free-text or missing status counts as open.
OPEN_TASKS = or_(CaseTask.status.is_(None), CaseTask.status.notin_(TASK_DONE))


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


ROUTES = ("sync", "person", "dormancy")


def clear_deactivation(user: User) -> None:
    """A reactivation: the route and the time are gone, so a later push can
    deactivate again and a sync push will not treat a person's deactivation as its own.
    """
    user.deactivation_route = None
    user.deactivated_at = None


def deactivate_account(
    session: Session,
    user: User,
    *,
    actor: str,
    by_person: bool,
    record_trail: bool = True,
    route: str | None = None,
) -> list[str]:
    """Deactivate ``user`` in ``session``; returns the credential ids revoked.

    ``by_person`` is whether a person at a screen did it. When not, the managers
    are told if the account owned open work. ``record_trail=False`` leaves the
    ``credential.revoked`` entries to the caller, for a disable that commits on
    its own ahead of the rest of a request.

    ``route`` is who did it (``sync``, ``person`` or ``dormancy``). Callers pass
    it. It is the only writer of ``deactivation_route`` and ``deactivated_at``.
    """
    if route is not None and route not in ROUTES:
        raise ValueError(f"unknown deactivation route {route}")
    user.is_active = False
    if route is not None:
        user.deactivation_route = route
        user.deactivated_at = utcnow()
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
    if record_trail:  # else the caller revokes its reset links and records them
        invites.revoke_resets(session, user.principal_id, actor, "deactivated")
    session.flush()
    if not by_person:
        _tell_managers_of_work(session, user)
    return revoked


def _tell_managers_of_work(session: Session, user: User) -> None:
    """One notice if the account owns open cases or tasks: counts only, no titles,
    the cases linked to the Cases list filtered to this owner. Sent on commit.

    Each manager is told what they may read: a restricted case they are not a
    reader of is counted apart, as hidden (#4356)."""
    cases = (
        session.query(Case.reader_principal_ids, Case.reader_team_ids)
        .filter(Case.assignee == user.username, Case.status != "closed")
        .all()
    )
    # Each open task with its case's reader lists: a task is read as its case is.
    tasks = (
        session.query(Case.reader_principal_ids, Case.reader_team_ids)
        .select_from(CaseTask)
        .join(Case, Case.case_id == CaseTask.case_id)
        .filter(CaseTask.assignee == user.username, OPEN_TASKS)
        .all()
    )
    if not (cases or tasks):
        return

    def message(principal: str) -> str | None:
        reader = case_reader_for(principal, session)
        shown = sum(1 for case in cases if may_read_case(case, reader))
        hidden = len(cases) - shown
        # Tasks on a case they can't read are left out, not counted.
        readable_tasks = sum(1 for task in tasks if may_read_case(task, reader))
        if not (shown or hidden or readable_tasks):
            return None  # nothing they may know of: no notice
        return _work_message(user.username, shown, hidden, readable_tasks)

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


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _work_message(username: str, cases: int, hidden: int, tasks: int) -> str:
    parts = []
    if cases:
        parts.append(_plural(cases, "open case"))
    if tasks:
        parts.append(_plural(tasks, "open task"))
    owns = " and ".join(parts) if parts else "open work"
    message = f"{username} was deactivated and owns {owns}."
    if hidden:
        message += f" {hidden} hidden: restricted open case{'s' if hidden != 1 else ''} you can't read."
    return message + " Reassign the work."


def tell_managers(
    session: Session,
    user: User,
    kind: str,
    title: str,
    message: Union[str, Callable[[str], str | None]],
    **notice,
) -> None:
    """A notice about ``user`` to each manager, once ``session`` commits.
    ``message`` may be worded per manager: a callable given their principal id,
    which returns ``None`` for a manager who is not to be told."""
    # Read now: the row is expired once the commit has happened.
    about = user.principal_id

    def send() -> None:
        from core.auth.auth_service import managers

        for principal in managers(about):
            try:
                text = message(principal) if callable(message) else message
                if text is None:
                    continue
                notify(principal, kind, title, text, about=about, **notice)
            except Exception:  # noqa: BLE001 - the others are still told
                logger.exception("could not send a %s notice to %s", kind, principal)

    when_committed(session, send)
