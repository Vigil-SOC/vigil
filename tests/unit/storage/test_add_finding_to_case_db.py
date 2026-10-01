"""add_finding_to_case writes the link row; its cost doesn't grow with the case."""

from datetime import timedelta

import pytest
from sqlalchemy import event, select

from core.storage.connection import get_db_manager
from core.storage.models import Case, Finding, case_findings
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _case(case_id, *finding_ids):
    stale = utcnow() - timedelta(days=1)
    with get_db_manager().session_scope() as session:
        case = Case(case_id=case_id, title="atc", updated_at=stale)
        for fid in finding_ids:
            case.findings.append(Finding(finding_id=fid, data_source="atc"))
        session.add(case)


def _finding(finding_id):
    with get_db_manager().session_scope() as session:
        session.add(Finding(finding_id=finding_id, data_source="atc"))


def _links(case_id):
    with get_db_manager().session_scope() as session:
        return sorted(
            session.execute(
                select(case_findings.c.finding_id).where(
                    case_findings.c.case_id == case_id
                )
            ).scalars()
        )


def _updated_at(case_id):
    with get_db_manager().session_scope() as session:
        return session.get(Case, case_id).updated_at


def test_links_once_and_bumps_updated_at_only_when_it_links():
    _case("case-atc-1")
    _finding("atc-f1")
    before = _updated_at("case-atc-1")

    assert DatabaseService().add_finding_to_case("case-atc-1", "atc-f1") is True
    linked_at = _updated_at("case-atc-1")
    assert linked_at > before

    # Already linked is still True, as before; nothing is written.
    assert DatabaseService().add_finding_to_case("case-atc-1", "atc-f1") is True
    assert _updated_at("case-atc-1") == linked_at
    assert _links("case-atc-1") == ["atc-f1"]


def test_a_missing_case_or_finding_is_false_and_links_nothing():
    _case("case-atc-2")
    _finding("atc-f2")

    assert DatabaseService().add_finding_to_case("case-atc-gone", "atc-f2") is False
    assert DatabaseService().add_finding_to_case("case-atc-2", "atc-gone") is False
    assert _links("case-atc-2") == []


def test_cost_does_not_grow_with_the_case():
    _case("case-atc-small", "atc-s0")
    _case("case-atc-big", *(f"atc-b{n}" for n in range(300)))
    _finding("atc-new-small")
    _finding("atc-new-big")

    cost = {}
    engine = get_db_manager().engine
    for case_id, finding_id in (
        ("case-atc-small", "atc-new-small"),
        ("case-atc-big", "atc-new-big"),
    ):
        seen = {"trips": 0, "rows": 0}

        def count(_conn, cursor, *_args):
            seen["trips"] += 1
            if cursor.description is not None:
                seen["rows"] += cursor.rowcount

        event.listen(engine, "after_cursor_execute", count)
        try:
            assert DatabaseService().add_finding_to_case(case_id, finding_id)
        finally:
            event.remove(engine, "after_cursor_execute", count)
        cost[case_id] = seen

    # The old path read every finding already on the case: 300 rows more here.
    assert cost["case-atc-big"] == cost["case-atc-small"]
