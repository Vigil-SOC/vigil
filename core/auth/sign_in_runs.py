"""What a sign-in's agent runs do when it ends for a stopping reason (#4173).

``sign_ins.STOPS_RUNS`` says which endings stop runs; ``sign_ins.end`` and
``end_all_for_user`` queue them and hand them here once their transaction has
committed. The runs stop through the one existing path, ``stop_run``. Nothing is
revoked: MCP credentials stay valid, the notice only counts them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable, Optional

from sqlalchemy import text

from core.auth import sign_ins
from core.workflows.workflow_run_service import IN_FLIGHT_STATUSES

logger = logging.getLogger(__name__)

STOP_REASON = sign_ins.STOP_REASON
_ACTOR = "system"


def inherited_sid(run_id: str) -> Optional[str]:
    """The sign-in a run started from, for a run that follows it (a handoff)."""
    from core.storage.connection import get_db_session

    with get_db_session() as session:
        return session.execute(
            text("SELECT sign_in_id FROM workflow_runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar()


def _live_runs(sign_in_ids: Iterable[str]) -> dict[str, str]:
    """Run id to a label, for the in-flight runs of these sign-ins and every child
    run below them. A child has a ledger of its own and no row of its own."""
    from core.storage.connection import get_db_session

    with get_db_session() as session:
        rows = session.execute(
            text(
                "WITH RECURSIVE tree(run_id) AS ("
                "  SELECT CAST(run_id AS text) FROM workflow_runs"
                "  WHERE sign_in_id = ANY(:sids) AND status = ANY(:live)"
                "  UNION"
                "  SELECT CAST(e.run_id AS text) FROM agent_events e"
                "  JOIN tree t ON e.kind = 'run'"
                "   AND e.payload->'parent'->>'run_id' = t.run_id"
                ") SELECT t.run_id, w.workflow_name FROM tree t"
                " LEFT JOIN workflow_runs w ON w.run_id = t.run_id"
            ),
            {"sids": list(sign_in_ids), "live": list(IN_FLIGHT_STATUSES)},
        ).all()
    return {run_id: name or "child run" for run_id, name in rows}


def _reject_pending(run_id: str) -> None:
    """A stopped run's open approvals are rejected, as a cancel does."""
    from core.response.approval_service import (
        ActionStatus,
        AlreadyDecided,
        ApprovalService,
    )

    approvals = ApprovalService()
    for action in approvals.list_actions(
        status=ActionStatus.PENDING, workflow_run_id=run_id
    ):
        try:
            approvals.reject_action(
                action.action_id, reason=STOP_REASON, rejected_by=_ACTOR
            )
        except AlreadyDecided:
            pass


def _stop(runs: dict[str, str]) -> dict[str, str]:
    """Stop each run; the ones that were stopped, with their labels."""
    from core.workflows.run_cancel import stop_run
    from core.workflows.workflow_run_service import WorkflowRunService

    service = WorkflowRunService()
    stopped: dict[str, str] = {}
    for run_id, label in runs.items():
        try:
            asked = stop_run(run_id, STOP_REASON, _ACTOR)["abort_queued"]
            _reject_pending(run_id)
            # Reads "stopped: sign-in ended" whether or not the worker got to it.
            finalized = service.finalize_run(
                run_id, status="cancelled", error=STOP_REASON
            )
        except Exception:  # noqa: BLE001 - one run must not keep the rest running
            logger.exception("could not stop run %s of an ended sign-in", run_id)
            continue
        if asked or finalized:
            stopped[run_id] = label
    return stopped


def _managers(person: str) -> list[str]:
    """Active people, other than this one, who hold users.write."""
    from core.auth.auth_service import AuthService
    from core.storage.connection import get_db_session
    from core.storage.models import User

    with get_db_session() as session:
        users = (
            session.query(User.user_id, User.principal_id)
            .filter(User.is_active.is_(True), User.service_account.isnot(True))
            .all()
        )
        return [
            principal
            for user_id, principal in users
            if principal != person
            and AuthService.check_permission(user_id, "users.write", session=session)
        ]


def _tell(
    person: str, name: str, reason: str, stopped: dict[str, str], credentials: int
) -> None:
    """One notice per ending event, to the person and to managers. After an erasure
    to managers only: nobody is left to read it as the person."""
    from core.platform.notices import notify

    why = sign_ins.REASON_WORDS.get(reason, sign_ins.UNNAMED_REASON)
    runs = "\n".join(f"- {label} ({run_id})" for run_id, label in stopped.items())
    count = len(stopped)
    message = (
        f"A sign-in of {name} ended ({why}), so {count} agent "
        f"run{'s' if count != 1 else ''} started from it "
        f"{'were' if count != 1 else 'was'} stopped:\n{runs}\n"
        f"{name} holds {credentials} MCP credential"
        f"{'s' if credentials != 1 else ''}. They are not revoked: revoke any you "
        "do not recognise in Settings."
    )
    title = "Agent runs stopped: a sign-in ended"
    to = _managers(person)
    if reason != sign_ins.ERASED:
        to.append(person)
    for principal in to:
        try:
            notify(
                principal,
                "runs_stopped",
                title,
                message,
                link="/settings",
                priority="high",
            )
        except Exception:  # noqa: BLE001 - the runs are already stopped
            logger.exception("could not tell %s that runs were stopped", principal)


def stop(pending: list[tuple[str, str, str]]) -> None:
    """Stop the runs of these (sign-in id, principal id, reason) endings. Never
    raises: the sign-in has ended either way."""
    try:
        from core.auth import mcp_credential_service

        by_person: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for sid, person, reason in pending:
            by_person[person].append((sid, reason))
        for person, ended in by_person.items():
            stopped = _stop(_live_runs(sid for sid, _ in ended))
            if not stopped:
                continue
            owner, name = _account(person)
            credentials = (
                len(mcp_credential_service.list_for_user(owner)) if owner else 0
            )
            _tell(person, name, ended[0][1], stopped, credentials)
    except Exception:  # noqa: BLE001
        logger.exception("could not stop the runs of an ended sign-in")


def _account(principal_id: str) -> tuple[Optional[str], str]:
    """The person's user id and the name a notice calls them."""
    from core.storage.connection import get_db_session
    from core.storage.models import User

    with get_db_session() as session:
        found = (
            session.query(User.user_id, User.username)
            .filter(User.principal_id == principal_id)
            .first()
        )
    return (found[0], found[1]) if found else (None, "a person")
