"""The case a person's start runs on, for both doors: HTTP execute and /mcp start_run.

- A named case the caller can't read is not found, as one that isn't there.
- ``restricted``: a named case must already be restricted; with none, a new case
  is opened restricted to the starter and the insider lead readers (#4778). The open case already holding the finding
  is never used, so a restricted run's notes don't land where anyone can read them.
- Otherwise, with none: the newest not-closed case holding the finding, else a new one.

A case opened for a start that made no run is deleted again.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from core.audit import trail

logger = logging.getLogger(__name__)

NOT_RESTRICTED = (
    "Case {case_id} is open to everyone, and a restricted start runs only on a "
    "restricted case: restrict it first, or start with no case to open a new one"
)


class StartRefused(Exception):
    """A start refused before any run: ``status`` is the HTTP code it answers with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _data() -> Any:
    from core.storage.database_data_service import get_database_data_service

    return get_database_data_service()


def _check_named(case_id: str, restricted: bool) -> None:
    from core.auth.permissions import case_restricted, may_read_case
    from core.storage.models import Case
    from core.storage.unit_of_work import unit_of_work

    if not _data().is_using_database():
        # Demo cases are not rows and are never restricted.
        if restricted:
            raise StartRefused(409, "Demo cases can't be restricted")
        return
    with unit_of_work() as session:
        case = session.get(Case, case_id)
        if case is None or not may_read_case(case):
            raise StartRefused(404, f"Case {case_id} not found")
        if restricted and not case_restricted(case):
            raise StartRefused(400, NOT_RESTRICTED.format(case_id=case_id))


def _readers(restricter: Callable[[Any], Any]) -> Tuple[list, list]:
    """The new case's lists: the starter, plus the insider lead readers and teams
    (#4778). A reader or team gone since the list was written is left off."""
    from sqlalchemy import select

    from core.cases import case_readers
    from core.storage.models import Team
    from core.storage.unit_of_work import unit_of_work
    from core.workflows.insider_lead import configured_readers

    if _data().is_demo_mode():
        raise StartRefused(409, "Demo cases can't be restricted")
    configured, team_ids = configured_readers()
    try:
        with unit_of_work() as session:
            actor = restricter(session)
            if not configured and not team_ids:
                return case_readers.readers_for_new_case(session, None, actor)
            names = case_readers._checked(
                session,
                [*case_readers.default_readers(session, None, actor), *configured],
                actor,
                drop=True,
            )
            teams = [
                t.team_id
                for t in session.scalars(
                    select(Team).where(
                        Team.team_id.in_(team_ids),
                        Team.archived_at.is_(None),
                        Team.is_default.is_(False),
                    )
                )
            ]
            if not names and not teams:
                raise case_readers.ReadersRefused(case_readers.NAME_ONE)
            return names, [t for t in team_ids if t in teams]
    except case_readers.ReadersRefused as exc:
        raise StartRefused(exc.status_code, exc.message) from None


def case_for_start(
    workflow_id: str,
    parameters: Dict[str, Any],
    restricter: Callable[[Any], Any],
    restricted: bool = False,
) -> Tuple[str, Optional[str]]:
    """The case this start runs on, and its id again if it was opened for it.

    ``restricter`` maps a session to the door's ``case_readers.Restricter``; it is
    asked only when a restricted case is opened.
    """
    from core.cases.case_sla_service import case_title, open_case, open_case_holding

    if case_id := parameters.get("case_id"):
        _check_named(case_id, restricted)
        return case_id, None

    readers = reader_teams = None
    if restricted:
        readers, reader_teams = _readers(restricter)
    finding_id = parameters.get("finding_id")
    try:
        if not restricted and finding_id and (holding := open_case_holding(finding_id)):
            return holding, None
        data = _data()
        finding = (data.get_finding(finding_id) if finding_id else None) or {}
        text = (
            parameters.get("hypothesis")
            or parameters.get("context")
            or finding.get("description")
        )
        opened = open_case(
            data,
            # The first line only: a lead proposal states one hypothesis per line.
            title=case_title(
                (text or finding.get("title") or workflow_id).splitlines()[0], 60
            ),
            finding_ids=[finding_id] if finding_id else [],
            priority="medium",
            description=parameters.get("hypothesis") or parameters.get("context") or "",
            readers=readers,
            reader_teams=reader_teams,
        )
    except trail.Paused:
        raise  # a 503 with Retry-After, as a comment is
    except Exception:
        logger.exception("could not open a case for a %s start", workflow_id)
        opened = None
    if not opened:
        raise StartRefused(500, "Could not open a case for this run")
    return opened["case_id"], opened["case_id"]


def drop_case(case_id: Optional[str]) -> None:
    """Delete the case opened for a start that never made a run."""
    if not case_id:
        return
    # A refused or paused trail append raises; the caller's own error stands.
    try:
        dropped = _data().delete_case(case_id)
    except Exception:  # noqa: BLE001
        dropped = False
    if not dropped:
        logger.error("could not delete case %s opened for a refused start", case_id)


async def start_on_case(
    workflow_id: str,
    parameters: Dict[str, Any],
    restricter: Callable[[Any], Any],
    execute: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Pick the case, run ``execute`` on the parameters, and drop the case again
    when it was opened here and no run row was made. ``restricted`` is read off
    ``parameters`` and never reaches the run. Returns the result with ``case_id``."""
    restricted = bool(parameters.pop("restricted", None))
    parameters["case_id"], opened = case_for_start(
        workflow_id, parameters, restricter, restricted
    )
    try:
        result = await execute(parameters)
    except Exception:
        drop_case(opened)
        raise
    # Once a run row exists the case stays: the failed run lives on it.
    if not result.get("run_id"):
        drop_case(opened)
    return {**result, "case_id": parameters["case_id"]}
