"""A status edit that closes or reopens a case, recorded the same way as the console.

Episodic memory reads closure rows as verdicts. An agent that only wrote
``cases.status`` left no row, so the verdict was re-derived from ``updated_at``,
which moves on every later edit.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from core.cases.case_workflow_service import CaseWorkflowService
from core.cases.closure import ClosedByKind, ClosureCategory
from core.integrations.mcp.surface import current_caller
from core.storage.connection import get_db_session

# No principal bound: nobody is behind the call, as in a hunt.
CALLER_UNAUTHENTICATED = "agent"


def actor() -> str:
    """Who this call writes into a record. Never an argument the model supplied."""
    return current_caller() or CALLER_UNAUTHENTICATED


@contextmanager
def service_session() -> Iterator:
    """A session of the caller's own, committed if the work returns.

    Tools reach the database directly rather than through the API, so each
    call owns its transaction. One definition, so commit means one thing.
    """
    session = get_db_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def record_agent_close(case_id: str) -> None:
    """Record that an agent closed this case and stated no category.

    ``unspecified`` is not a determination. An agent that has one calls
    ``close_case`` and says which. This does not overwrite a determination
    already on record. ``closed_by_kind`` is ``agent`` even over HTTP: a
    credential here is one a program holds.
    """
    with service_session() as session:
        CaseWorkflowService().close_case(
            session,
            case_id,
            closure_category=ClosureCategory.UNSPECIFIED,
            closed_by=actor(),
            closed_by_kind=ClosedByKind.AGENT,
        )


def record_reopen(case_id: str) -> None:
    """Retract the category a close recorded. The write-up stays."""
    with service_session() as session:
        CaseWorkflowService().reopen_case(session, case_id)
