"""Tests for the consolidated Case repository."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from core.storage.case_repository import CaseRepository, _as_list

pytestmark = pytest.mark.unit


def _compile(stmt):
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class _FakeCase:
    def __init__(self):
        self.findings = []


class _FakeFinding:
    def __init__(self, finding_id):
        self.finding_id = finding_id


def test_as_list_normalizes():
    assert _as_list(None) == []
    assert _as_list("x") == ["x"]
    assert _as_list(["a", "b"]) == ["a", "b"]


def test_set_findings_resolves_into_relationship():
    case = _FakeCase()
    findings = [_FakeFinding("f1"), _FakeFinding("f2")]
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = findings

    CaseRepository(session).set_findings(case, ["f1", "f2"])

    assert case.findings == findings


def test_set_findings_empty_skips_query():
    case = _FakeCase()
    case.findings = [_FakeFinding("f1")]
    session = MagicMock()

    CaseRepository(session).set_findings(case, [])

    assert case.findings == []
    session.execute.assert_not_called()  # no query for an empty id list


def test_build_scalar_and_list_filters_are_equivalent():
    repo = CaseRepository(MagicMock())
    # A scalar and a single-element list should compile the same way.
    scalar_sql = _compile(repo._build(status="new"))
    list_sql = _compile(repo._build(status=["new"]))
    assert scalar_sql == list_sql
    assert "cases.status IN ('new')" in scalar_sql


def test_build_full_text_and_dates():
    repo = CaseRepository(MagicMock())
    sql = _compile(
        repo._build(
            query_text="phish",
            priority=["high", "critical"],
            created_after=datetime(2026, 1, 1),
        )
    )
    assert "cases.title ILIKE '%%phish%%'" in sql
    assert "cases.priority IN ('high', 'critical')" in sql
    assert "cases.created_at >=" in sql


def test_build_sla_breach_joins():
    repo = CaseRepository(MagicMock())
    breached = _compile(repo._build(has_sla_breach=True))
    assert "JOIN case_slas" in breached
    assert "case_slas.breached IS true" in breached

    not_breached = _compile(repo._build(has_sla_breach=False))
    assert "LEFT OUTER JOIN case_slas" in not_breached
