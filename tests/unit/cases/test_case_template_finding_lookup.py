"""Creating a case from a template looks its findings up in one query.

``create_case_from_template`` used to run one ``SELECT ... WHERE finding_id =``
per id, and each of those pulled the finding's cases and MITRE predictions
in two more, so linking 30 findings cost 90 statements. It now resolves the
ids with a single ``IN`` query. The linked findings must not change: input
order, a repeated id, and an unknown id all behave as they did before.
"""

import re
import uuid
from typing import Iterator, List

import pytest
from sqlalchemy import event, select

from core.cases.case_workflow_service import CaseWorkflowService
from core.storage.connection import get_db_session
from core.storage.models import CaseTemplate, Finding, case_findings
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# A lookup by bound id, ``= %(...)s`` or ``IN (...)``, not the join to
# case_findings that loads the new case's own collection.
_FINDING_LOOKUP = re.compile(r"\bfindings\.finding_id (?:= %|IN \()", re.IGNORECASE)


@pytest.fixture
def prefix() -> Iterator[str]:
    """A unique id prefix; the findings seeded under it are removed after."""
    value = f"tmpl-lookup-{uuid.uuid4().hex[:8]}"
    yield value
    db = get_db_session()
    try:
        db.query(Finding).filter(Finding.data_source == value).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def session(prefix):
    """A session holding an active template, rolled back afterwards."""
    db = get_db_session()
    db.add(
        CaseTemplate(
            template_id=prefix,
            name=prefix,
            template_type="test",
            is_active=True,
        )
    )
    db.flush()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _seed(prefix: str, count: int) -> List[str]:
    service = DatabaseService()
    ids = [f"{prefix}-{i}" for i in range(count)]
    for finding_id in ids:
        assert service.create_finding(
            finding_id=finding_id,
            mitre_predictions={},
            anomaly_score=0.5,
            timestamp=utcnow(),
            data_source=prefix,
            severity="medium",
            status="new",
        )
    return ids


def _create(session, prefix: str, finding_ids: List[str]):
    return CaseWorkflowService().create_case_from_template(
        prefix, f"{prefix} case", finding_ids=finding_ids, session=session
    )


def _statements_while_creating(session, prefix, finding_ids) -> List[str]:
    statements: List[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", before)
    try:
        case = _create(session, prefix, finding_ids)
    finally:
        event.remove(conn, "before_cursor_execute", before)
    assert case is not None
    assert [f.finding_id for f in case.findings] == finding_ids
    # Each count gets its own case; the case id is only second-precise.
    session.rollback()
    return statements


def test_finding_lookup_is_one_query_whatever_the_count(prefix, session):
    ids = _seed(prefix, 30)

    few = _statements_while_creating(session, prefix, ids[:3])
    session.add(
        CaseTemplate(
            template_id=prefix, name=prefix, template_type="test", is_active=True
        )
    )
    session.flush()
    many = _statements_while_creating(session, prefix, ids)

    lookups = (
        len([s for s in few if _FINDING_LOOKUP.search(s)]),
        len([s for s in many if _FINDING_LOOKUP.search(s)]),
    )
    assert lookups == (1, 1)
    assert len(many) == len(few)


def test_links_keep_input_order_repeats_and_skip_unknown_ids(prefix, session):
    first, second, third = _seed(prefix, 3)
    requested = [third, f"{prefix}-missing", first, third, second]

    case = _create(session, prefix, requested)

    assert case is not None
    assert [f.finding_id for f in case.findings] == [third, first, third, second]

    session.flush()
    linked = (
        session.execute(
            select(case_findings.c.finding_id).where(
                case_findings.c.case_id == case.case_id
            )
        )
        .scalars()
        .all()
    )
    assert sorted(linked) == sorted([first, second, third])


def test_unknown_ids_only_link_nothing(prefix, session):
    case = _create(session, prefix, [f"{prefix}-nope", f"{prefix}-gone"])

    assert case is not None
    assert case.findings == []
