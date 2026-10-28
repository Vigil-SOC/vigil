"""``GET /api/cases/{id}/relationships`` names each related Case (#3293).

DB-backed: the related Case's title and status come from a join, and a link
to a deleted Case is dropped by it, which a fake session cannot show.
"""

from __future__ import annotations

import pytest

from core.cases import case_records_service
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseRelationship
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(CaseRelationship).delete()
        db.query(Case).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def _case(session, case_id: str, status: str = "open") -> None:
    session.add(
        Case(
            case_id=case_id,
            title=f"title of {case_id}",
            status=status,
            priority="medium",
            created_at=utcnow(),
        )
    )
    session.flush()


@pytest.mark.asyncio
async def test_each_link_names_its_case_and_a_deleted_one_is_gone(session):
    from services.api.routers import cases

    _case(session, "case-a")
    _case(session, "case-b", status="closed")
    _case(session, "case-c")
    for related, note in (("case-b", "same host"), ("case-c", "")):
        case_records_service.add_relationship(
            session,
            "case-a",
            related_case_id=related,
            relationship_type="related_to",
            created_by="alice",
            notes=note,
        )
    session.delete(session.get(Case, "case-c"))
    session.flush()

    body = await cases.get_relationships("case-a", session)

    assert [
        (r["related_case_id"], r["related_title"], r["related_status"], r["notes"])
        for r in body["relationships"]
    ] == [("case-b", "title of case-b", "closed", "same host")]
