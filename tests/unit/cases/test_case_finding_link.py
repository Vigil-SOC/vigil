"""Regression test for the case<->finding link drop.

``DatabaseService.update_case`` used to update fields with a generic
``for key, value in updates.items(): if hasattr(case, key): setattr(...)``
loop. ``finding_ids`` is not a mapped column (the link is the ``findings``
relationship via ``case_findings``), so ``hasattr(case, "finding_ids")`` was
False and the update was silently dropped on Postgres — the method still
returned True, so nothing surfaced the loss.

These tests pin the fix: ``finding_ids`` is resolved into ``case.findings``.
They use a fake session so they run without a live database.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from core.storage.service import DatabaseService

pytestmark = pytest.mark.unit


class _FakeCase:
    """Stand-in for the ORM Case: has a ``findings`` relationship list but,
    like the real model, no ``finding_ids`` attribute."""

    def __init__(self):
        self.findings = []
        self.updated_at = None
        self.status = "new"


class _FakeFinding:
    def __init__(self, finding_id):
        self.finding_id = finding_id


def _service_with_session(session):
    """Build a DatabaseService whose ``session_scope()`` yields ``session``,
    skipping the real ``get_db_manager()`` wiring in ``__init__``."""
    svc = DatabaseService.__new__(DatabaseService)

    @contextmanager
    def _scope():
        yield session

    svc.db_manager = MagicMock()
    svc.db_manager.session_scope.side_effect = lambda: _scope()
    return svc


def _fake_session(case):
    session = MagicMock()
    session.get.return_value = case
    return session


def test_update_case_persists_finding_ids():
    """finding_ids resolves into the findings relationship (was dropped)."""
    case = _FakeCase()
    findings = [_FakeFinding("f1"), _FakeFinding("f2")]

    session = _fake_session(case)
    session.execute.return_value.scalars.return_value.all.return_value = findings

    svc = _service_with_session(session)
    ok = svc.update_case("c1", finding_ids=["f1", "f2"])

    assert ok is True
    assert case.findings == findings
    # The link must go through the relationship, never a stray scalar attribute.
    assert not hasattr(case, "finding_ids")


def test_update_case_clears_finding_ids_when_empty():
    """An empty finding_ids list unlinks all findings (no query needed)."""
    case = _FakeCase()
    case.findings = [_FakeFinding("f1")]

    session = _fake_session(case)
    session.execute.return_value.scalars.return_value.all.return_value = []

    svc = _service_with_session(session)
    ok = svc.update_case("c1", finding_ids=[])

    assert ok is True
    assert case.findings == []


def test_update_case_still_updates_mapped_fields():
    """Regular mapped fields keep working alongside the finding_ids handling."""
    case = _FakeCase()
    session = _fake_session(case)

    svc = _service_with_session(session)
    ok = svc.update_case("c1", status="closed")

    assert ok is True
    assert case.status == "closed"


def test_update_case_missing_case_returns_false():
    """Unknown case id short-circuits to False."""
    session = _fake_session(case=None)

    svc = _service_with_session(session)
    assert svc.update_case("missing", finding_ids=["f1"]) is False
