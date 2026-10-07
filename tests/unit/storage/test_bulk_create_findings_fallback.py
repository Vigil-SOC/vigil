"""bulk_create_findings' row-by-row fallback, without a database (#1434).

The PostgreSQL-backed tests in test_bulk_create_findings_db.py exercise the
real column limits; these pin the fallback's accounting and its refusal to
retry row by row when the database itself is unreachable.
"""

from typing import Any, Dict, List

import pytest
from sqlalchemy.exc import DataError, OperationalError

from core.storage.service import DatabaseService

pytestmark = pytest.mark.unit


class _FakeInsert:
    """Stands in for _insert_new_findings: fails any call holding a bad id."""

    def __init__(self, bad: set, existing: set = frozenset(), error=DataError):
        self.bad = bad
        self.existing = existing
        self.error = error
        self.calls: List[List[str]] = []

    def __call__(self, rows: List[Dict[str, Any]]) -> int:
        ids = [r["finding_id"] for r in rows]
        self.calls.append(ids)
        if self.bad & set(ids):
            raise self.error("INSERT", {}, Exception("value too long"))
        return len([i for i in ids if i not in self.existing])


def _service(monkeypatch, fake: _FakeInsert) -> DatabaseService:
    service = DatabaseService.__new__(DatabaseService)
    monkeypatch.setattr(service, "_insert_new_findings", fake, raising=False)
    return service


def test_happy_path_is_one_transaction(monkeypatch):
    fake = _FakeInsert(bad=set(), existing={"b"})
    service = _service(monkeypatch, fake)

    result = service.bulk_create_findings(
        [{"finding_id": i} for i in ("a", "b", "c", "c")]
    )

    assert result == {"imported": 2, "skipped": 2}
    assert fake.calls == [["a", "b", "c"]]


def test_failed_batch_falls_back_to_rows(monkeypatch):
    fake = _FakeInsert(bad={"bad"}, existing={"old"})
    service = _service(monkeypatch, fake)
    ids = ["a", "bad", "old", "b", "b"]

    result = service.bulk_create_findings([{"finding_id": i} for i in ids])

    assert result == {"imported": 2, "skipped": 2, "errors": 1}
    assert fake.calls == [["a", "bad", "old", "b"], ["a"], ["bad"], ["old"], ["b"]]


def test_unreachable_database_is_not_retried_row_by_row(monkeypatch):
    fake = _FakeInsert(bad={"a"}, error=OperationalError)
    service = _service(monkeypatch, fake)

    result = service.bulk_create_findings([{"finding_id": i} for i in "abc"])

    assert result == {"imported": 0, "skipped": 0, "errors": 3}
    assert len(fake.calls) == 1


def test_connection_lost_mid_fallback_counts_the_rest(monkeypatch):
    fake = _FakeInsert(bad={"bad"})
    service = _service(monkeypatch, fake)

    def insert(rows):
        if rows[0]["finding_id"] == "c":
            raise OperationalError("INSERT", {}, Exception("server closed"))
        return fake(rows)

    monkeypatch.setattr(service, "_insert_new_findings", insert, raising=False)

    result = service.bulk_create_findings(
        [{"finding_id": i} for i in ("a", "bad", "c", "d")]
    )

    assert result == {"imported": 1, "skipped": 0, "errors": 3}
