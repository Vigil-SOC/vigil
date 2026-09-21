"""A deactivated SLA policy stops being assigned, however it is named.

Deleting a policy a case references is refused by a foreign key, so the answer
an operator is given is to deactivate it instead -- "no new case takes it". That
promise only holds if a policy named explicitly is filtered on ``is_active`` the
same way the default lookup is. A case template carries
``default_sla_policy_id`` and ``core/cases/case_workflow_service.py`` passes it
straight into the explicit branch, so without that filter the advice is false
and the policy keeps being attached.

DB-backed on purpose: the rule lives entirely in a ``WHERE`` clause, and a fake
session that ignores ``filter()`` asserts nothing about it.
"""

from __future__ import annotations

import pytest

from core.cases.case_sla_service import CaseSLAService
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseSLA, SLAPolicy
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

ACTIVE = "sla-test-active"
RETIRED = "sla-test-retired"
CASE_ID = "case-sla-inactive"


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(CaseSLA).filter(CaseSLA.case_id == CASE_ID).delete()
        db.query(Case).filter(Case.case_id == CASE_ID).delete()
        db.query(SLAPolicy).filter(SLAPolicy.policy_id.in_([ACTIVE, RETIRED])).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.query(CaseSLA).filter(CaseSLA.case_id == CASE_ID).delete()
        db.query(Case).filter(Case.case_id == CASE_ID).delete()
        db.query(SLAPolicy).filter(SLAPolicy.policy_id.in_([ACTIVE, RETIRED])).delete()
        db.commit()
        db.close()


def _policy(session, policy_id: str, *, is_active: bool) -> SLAPolicy:
    policy = SLAPolicy(
        policy_id=policy_id,
        name=policy_id,
        priority_level="high",
        response_time_hours=1.0,
        resolution_time_hours=8.0,
        is_active=is_active,
    )
    session.add(policy)
    session.flush()
    return policy


def _case(session) -> Case:
    case = Case(
        case_id=CASE_ID,
        title="a case needing an SLA",
        status="open",
        priority="high",
        created_at=utcnow(),
    )
    session.add(case)
    session.flush()
    return case


def test_a_policy_named_outright_is_still_refused_once_deactivated(session):
    """The case that makes 'deactivate it instead' true: a template names it."""
    _case(session)
    _policy(session, RETIRED, is_active=False)
    session.flush()

    assigned = CaseSLAService().assign_sla_to_case(
        CASE_ID, sla_policy_id=RETIRED, session=session
    )

    assert assigned is None, (
        "A deactivated policy was assigned to a case that named it explicitly. "
        "Deactivating is what an operator is told to do with a policy they "
        "cannot delete, so the explicit branch has to filter on is_active like "
        "the default lookup does."
    )


def test_an_active_policy_named_outright_is_still_assigned(session):
    """Guards the guard: the filter must not refuse every named policy."""
    _case(session)
    _policy(session, ACTIVE, is_active=True)
    session.flush()

    assigned = CaseSLAService().assign_sla_to_case(
        CASE_ID, sla_policy_id=ACTIVE, session=session
    )

    assert assigned is not None
    assert assigned.sla_policy_id == ACTIVE
