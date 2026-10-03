"""``get_related_cases`` loads the top-N related cases in one query.

The scoring pass used to be followed by one ``SELECT ... WHERE case_id = ?``
per result, so the statement count grew with ``max_results``. These tests pin
the count and the output (scores, reasons, order and tie-breaking).

Every row is created inside the test's transaction with values unique to the
run, and rolled back afterwards, so nothing else in the database can match.
"""

import re
import uuid

import pytest
from sqlalchemy import event

from core.cases.case_search_service import CaseSearchService
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseIOC

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def session():
    db = get_db_session()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def tag():
    return uuid.uuid4().hex[:8]


def _case(session, case_id, *, techniques=None, iocs=()):
    session.add(Case(case_id=case_id, title=case_id, mitre_techniques=techniques or []))
    session.flush()
    for value in iocs:
        session.add(CaseIOC(case_id=case_id, ioc_type="ip", value=value))
    session.flush()


def _count_queries(session, fn):
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", before)
    try:
        result = fn()
    finally:
        event.remove(conn, "before_cursor_execute", before)
    return statements, result


def _case_lookups(statements):
    """Statements that load ``cases`` rows by ``case_id``.

    One is the source-case lookup; the rest load the related cases. The
    eager ``case_findings`` load is a JOIN and is not counted here.
    """
    return [s for s in statements if re.search(r"FROM cases\s+WHERE cases\.case_id", s)]


def _seed_related(session, tag, n):
    base = f"rc-{tag}-base"
    shared = f"ioc-{tag}"
    _case(session, base, iocs=[shared])
    for i in range(n):
        _case(session, f"rc-{tag}-{i:02d}", iocs=[shared])
    return base


@pytest.mark.parametrize("n", [2, 10])
def test_result_lookup_is_one_query(session, tag, n):
    base = _seed_related(session, tag, n)
    service = CaseSearchService()

    statements, result = _count_queries(
        session, lambda: service.get_related_cases(base, 10, session=session)
    )

    assert len(result) == n
    # The source case, then ONE batch lookup for all related cases.
    assert len(_case_lookups(statements)) == 2


def test_query_count_does_not_grow_with_results(session, tag):
    service = CaseSearchService()
    small = _seed_related(session, tag + "s", 2)
    large = _seed_related(session, tag + "l", 10)

    small_statements, _ = _count_queries(
        session, lambda: service.get_related_cases(small, 10, session=session)
    )
    large_statements, _ = _count_queries(
        session, lambda: service.get_related_cases(large, 10, session=session)
    )

    assert len(small_statements) == len(large_statements)


def test_scores_reasons_and_order(session, tag):
    v1, v2, v3 = (f"ioc-{tag}-{i}" for i in range(1, 4))
    base = f"rc-{tag}-base"
    _case(session, base, iocs=[v1, v2, v3])
    # Seeded out of score order, so the output order has to come from scores.
    _case(session, f"rc-{tag}-c", iocs=[v3])
    _case(session, f"rc-{tag}-a", iocs=[v1, v2, v3])
    _case(session, f"rc-{tag}-b", iocs=[v1, v2])
    _case(session, f"rc-{tag}-z", iocs=[f"ioc-{tag}-unrelated"])

    service = CaseSearchService()
    result = service.get_related_cases(base, 10, session=session)

    assert [
        (r["case_id"], r["similarity_score"], r["similarity_reasons"]) for r in result
    ] == [
        (f"rc-{tag}-a", 30, ["shared_ioc"] * 3),
        (f"rc-{tag}-b", 20, ["shared_ioc"] * 2),
        (f"rc-{tag}-c", 10, ["shared_ioc"]),
    ]
    # Full case payload, not just the scoring fields.
    assert result[0]["title"] == f"rc-{tag}-a"

    truncated = service.get_related_cases(base, 2, session=session)
    assert [r["case_id"] for r in truncated] == [f"rc-{tag}-a", f"rc-{tag}-b"]


def test_unknown_case_and_no_relations(session, tag):
    service = CaseSearchService()
    assert service.get_related_cases(f"rc-{tag}-missing", session=session) == []

    lonely = f"rc-{tag}-lonely"
    _case(session, lonely, iocs=[f"ioc-{tag}-x"])
    assert service.get_related_cases(lonely, session=session) == []


def test_source_with_techniques_does_not_raise(session, tag):
    # Generic ``sqlalchemy.ARRAY`` has no ``.overlap()``; the MITRE branch
    # used to raise AttributeError whenever the source case had techniques.
    base = f"rc-{tag}-base"
    _case(session, base, techniques=[f"T{tag}-1"])
    service = CaseSearchService()
    assert service.get_related_cases(base, session=session) == []


def test_shared_mitre_scores_five_per_technique(session, tag):
    t1, t2, t3 = (f"T{tag}-{i}" for i in range(1, 4))
    base = f"rc-{tag}-base"
    _case(session, base, techniques=[t1, t2, t3])
    _case(session, f"rc-{tag}-one", techniques=[t3, f"T{tag}-other"])
    _case(session, f"rc-{tag}-two", techniques=[t1, t2])
    _case(session, f"rc-{tag}-none", techniques=[f"T{tag}-unrelated"])

    result = CaseSearchService().get_related_cases(base, session=session)

    assert [
        (r["case_id"], r["similarity_score"], r["similarity_reasons"]) for r in result
    ] == [
        (f"rc-{tag}-two", 10, ["shared_mitre_techniques"]),
        (f"rc-{tag}-one", 5, ["shared_mitre_techniques"]),
    ]


def test_ioc_and_mitre_scores_combine_in_order(session, tag):
    v1, v2 = f"ioc-{tag}-1", f"ioc-{tag}-2"
    t1, t2, t3 = (f"T{tag}-{i}" for i in range(1, 4))
    base = f"rc-{tag}-base"
    _case(session, base, techniques=[t1, t2, t3], iocs=[v1, v2])
    # Seeded out of score order, so the output order has to come from scores.
    _case(session, f"rc-{tag}-mitre", techniques=[t1, t2, t3])  # 15
    _case(session, f"rc-{tag}-ioc", iocs=[v1])  # 10
    _case(session, f"rc-{tag}-both", techniques=[t1], iocs=[v1, v2])  # 25
    _case(session, f"rc-{tag}-tiny", techniques=[t2])  # 5

    result = CaseSearchService().get_related_cases(base, session=session)

    assert [
        (r["case_id"], r["similarity_score"], r["similarity_reasons"]) for r in result
    ] == [
        (
            f"rc-{tag}-both",
            25,
            ["shared_ioc", "shared_ioc", "shared_mitre_techniques"],
        ),
        (f"rc-{tag}-mitre", 15, ["shared_mitre_techniques"]),
        (f"rc-{tag}-ioc", 10, ["shared_ioc"]),
        (f"rc-{tag}-tiny", 5, ["shared_mitre_techniques"]),
    ]
