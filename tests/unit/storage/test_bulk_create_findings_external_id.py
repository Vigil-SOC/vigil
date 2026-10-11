"""Re-uploads dedupe on (data_source, external_id), not only finding_id (#1935).

A re-uploaded alerts file carries new finding_ids but the same external
ids. Those rows used to pass every dedup check and die on the
uniq_findings_source_extid index at flush, failing the upload with the raw
driver error. These tests pin, without a database (the Postgres-backed
suite is test_bulk_create_findings_db.py):

- a batch repeating a (source, external_id) pair inserts it once and
  counts the repeat as skipped;
- _insert_new_findings leaves out rows whose (source, external_id) is
  already stored;
- a stored-row failure reports a plain first_error, never driver text.
"""

from contextlib import contextmanager
from typing import Any, Dict, List

import pytest
from sqlalchemy.exc import DataError

from core.storage.service import DatabaseService

pytestmark = pytest.mark.unit

RAW = "RAWDRIVER psycopg2 UniqueViolation duplicate key"


def _row(finding_id: str, **overrides):
    row = {
        "finding_id": finding_id,
        "data_source": "okta",
        "external_id": f"ext-{finding_id}",
        "severity": "high",
    }
    row.update(overrides)
    return row


class _FakeInsert:
    """Stands in for _insert_new_findings, as in the fallback tests."""

    def __init__(self, bad: set = frozenset(), error=DataError):
        self.bad = bad
        self.error = error
        self.calls: List[List[str]] = []

    def __call__(self, rows: List[Dict[str, Any]]) -> int:
        ids = [r["finding_id"] for r in rows]
        self.calls.append(ids)
        if self.bad & set(ids):
            raise self.error("INSERT", {}, Exception(RAW))
        return len(ids)


def _service(monkeypatch, fake: _FakeInsert) -> DatabaseService:
    service = DatabaseService.__new__(DatabaseService)
    monkeypatch.setattr(service, "_insert_new_findings", fake, raising=False)
    return service


def test_batch_repeat_of_an_external_id_is_inserted_once(monkeypatch):
    fake = _FakeInsert()
    service = _service(monkeypatch, fake)

    result = service.bulk_create_findings(
        [
            _row("new-1", external_id="okta-9001"),
            _row("new-2", external_id="okta-9001"),  # same source + external id
            _row("new-3", external_id="okta-9001", data_source="splunk"),
            _row("new-4", external_id=None),
        ]
    )

    assert result == {"imported": 3, "skipped": 1}
    assert fake.calls == [["new-1", "new-3", "new-4"]]


def test_blank_external_ids_are_not_dedup_keys(monkeypatch):
    fake = _FakeInsert()
    service = _service(monkeypatch, fake)

    result = service.bulk_create_findings(
        [_row("a", external_id=""), _row("b", external_id="")]
    )

    assert result == {"imported": 2, "skipped": 0}
    assert fake.calls == [["a", "b"]]


def test_fallback_counts_external_repeat_as_skipped_and_error_stays_plain(
    monkeypatch,
):
    fake = _FakeInsert(bad={"bad"})
    service = _service(monkeypatch, fake)

    result = service.bulk_create_findings(
        [
            _row("new-1", external_id="okta-9001"),
            _row("new-2", external_id="okta-9001"),
            _row("bad"),
        ]
    )

    first_error = result.pop("first_error")
    assert result == {"imported": 1, "skipped": 1, "errors": 1}
    assert "psycopg2" not in first_error
    assert "RAWDRIVER" not in first_error


# ---------------------------------------------------------------------------
# _insert_new_findings against a stub session: stored (source, external_id)
# ---------------------------------------------------------------------------


class _FakeSession:
    """execute() answers the finding_id lookup, then the external-key one."""

    def __init__(self, stored_ids, stored_keys):
        self._answers = [list(stored_ids), list(stored_keys)]
        self.added: list = []

    def execute(self, _stmt):
        return iter(self._answers.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


class _FakeDbManager:
    def __init__(self, session):
        self._session = session

    @contextmanager
    def session_scope(self):
        yield self._session


def _insert_service(session) -> DatabaseService:
    service = DatabaseService.__new__(DatabaseService)
    service.db_manager = _FakeDbManager(session)
    return service


def test_insert_skips_rows_whose_external_id_is_already_stored():
    session = _FakeSession(
        stored_ids=[],
        stored_keys=[("okta", "okta-9001")],
    )
    service = _insert_service(session)

    inserted = service._insert_new_findings(
        [
            _row("new-1", external_id="okta-9001"),  # already stored
            _row("new-2", external_id="okta-9002"),
            _row("new-3", external_id=None),
            _row("new-4", external_id="okta-9001", data_source="splunk"),
        ]
    )

    assert inserted == 3
    assert [f.finding_id for f in session.added] == ["new-2", "new-3", "new-4"]


def test_insert_skips_a_repeat_external_id_inside_the_rows():
    session = _FakeSession(stored_ids=[], stored_keys=[])
    service = _insert_service(session)

    inserted = service._insert_new_findings(
        [
            _row("new-1", external_id="okta-9001"),
            _row("new-2", external_id="okta-9001"),
        ]
    )

    assert inserted == 1
    assert [f.finding_id for f in session.added] == ["new-1"]


def test_get_finding_by_external_id_returns_the_stored_row():
    sentinel = object()

    class _Scalars:
        def first(self):
            return sentinel

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        def execute(self, _stmt):
            return _Result()

        def expunge(self, _obj):
            pass

    service = _insert_service(_Session())

    assert service.get_finding_by_external_id("okta", "okta-9001") is sentinel
