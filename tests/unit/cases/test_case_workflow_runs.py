"""A case finds its execute-path runs through ``trigger_context.case_id``.

DB-backed on purpose: the case scoping lives entirely in the JSONB
``WHERE`` clause, and a fake session that ignores ``filter()`` asserts
nothing about it. A ``/hunt`` run has no ``Investigation`` row; this read
is how its case page sees it (#1858).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.cases import case_records_service
from core.storage.connection import get_db_session
from core.storage.models import WorkflowRun
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(WorkflowRun).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def _run(session, run_id, case_id, *, deleted=False, age_hours=0):
    run = WorkflowRun(
        run_id=run_id,
        workflow_id="threat-hunt",
        workflow_name="Threat hunt",
        status="running",
        trigger_context={"case_id": case_id} if case_id else {"hypothesis": "x"},
        started_at=utcnow() - timedelta(hours=age_hours),
        deleted_at=utcnow() if deleted else None,
    )
    session.add(run)
    session.flush()
    return run


def test_only_this_cases_runs_come_back_newest_first(session):
    _run(session, "wfr-old", "case-1", age_hours=3)
    _run(session, "wfr-new", "case-1", age_hours=1)
    _run(session, "wfr-other", "case-2", age_hours=0)
    _run(session, "wfr-caseless", None, age_hours=0)

    runs = case_records_service.list_case_workflow_runs(session, "case-1")

    assert [run.run_id for run in runs] == ["wfr-new", "wfr-old"]


def test_a_soft_deleted_run_stays_hidden(session):
    _run(session, "wfr-gone", "case-1", deleted=True)
    _run(session, "wfr-kept", "case-1")

    runs = case_records_service.list_case_workflow_runs(session, "case-1")

    assert [run.run_id for run in runs] == ["wfr-kept"]
