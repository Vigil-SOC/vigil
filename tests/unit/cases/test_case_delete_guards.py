"""A Case with a live Investigation cannot be deleted one at a time (#1001).

``DELETE /api/cases/{id}`` returns 409 while a finding-run is live on that
Case. Bulk reset kills those runs first. Hunts have no Case, so they never
block a single delete and are not killed by the bulk path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from core.cases import case_records_service
from core.storage.models import Investigation

pytestmark = pytest.mark.unit


class _Inv:
    def __init__(self, investigation_id, case_id, status, workdir=""):
        self.investigation_id = investigation_id
        self.case_id = case_id
        self.status = status
        self.master_review_notes = None
        self.workdir = workdir


class _Query:
    def __init__(self, rows, *, count=None):
        self.rows = list(rows)
        self._count = count
        self.update_calls = []

    def filter(self, *args, **kwargs):
        return self

    def count(self):
        return self._count if self._count is not None else len(self.rows)

    def all(self):
        return list(self.rows)

    def delete(self, **kwargs):
        return 0

    def update(self, values, **kwargs):
        self.update_calls.append(values)
        return 0


class _Session:
    def __init__(self, *, live_rows=(), case_count=0):
        self.live_rows = list(live_rows)
        self.case_count = case_count
        self.investigation_queries = []
        self.flushed = False

    def query(self, model):
        if model is Investigation:
            q = _Query(self.live_rows)
            self.investigation_queries.append(q)
            return q
        return _Query([], count=self.case_count)

    def execute(self, *_args, **_kwargs):
        return None

    def flush(self):
        self.flushed = True


def _delete_route(monkeypatch, *, case, live):
    from services.api.routers import cases

    monkeypatch.setattr(cases.data_service, "get_case", lambda case_id: case)
    monkeypatch.setattr(cases.data_service, "delete_case", MagicMock(return_value=True))
    monkeypatch.setattr(
        cases.case_records_service,
        "count_live_investigations",
        lambda session, case_id: live,
    )
    return cases


@pytest.mark.asyncio
async def test_delete_case_with_live_investigation_is_409(monkeypatch):
    cases = _delete_route(monkeypatch, case={"case_id": "case-1"}, live=1)

    with pytest.raises(HTTPException) as exc:
        await cases.delete_case("case-1", MagicMock())

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "case has 1 live investigations; kill or finish them first"
    )
    cases.data_service.delete_case.assert_not_called()


@pytest.mark.asyncio
async def test_delete_case_without_live_investigation_deletes(monkeypatch):
    cases = _delete_route(monkeypatch, case={"case_id": "case-1"}, live=0)

    result = await cases.delete_case("case-1", MagicMock())

    assert result == {"success": True}
    cases.data_service.delete_case.assert_called_once_with("case-1")


@pytest.mark.asyncio
async def test_delete_case_is_not_blocked_by_a_hunt(monkeypatch):
    """Hunts have no Case, so they are not in the live count."""
    cases = _delete_route(monkeypatch, case={"case_id": "case-1"}, live=0)

    await cases.delete_case("case-1", MagicMock())

    cases.data_service.delete_case.assert_called_once_with("case-1")


def test_count_live_investigations_is_scoped_to_the_case():
    session = MagicMock()
    chain = session.query.return_value
    chain.filter.return_value = chain
    chain.count.return_value = 2

    n = case_records_service.count_live_investigations(session, "case-1")

    assert n == 2
    session.query.assert_called_once_with(Investigation)
    args, _ = chain.filter.call_args
    assert any("case_id" in str(a) for a in args)
    assert any("status" in str(a) for a in args)


def test_kill_query_excludes_caseless_rows():
    session = MagicMock()
    chain = session.query.return_value
    chain.filter.return_value = chain
    chain.all.return_value = []

    case_records_service.kill_live_case_investigations(session)

    args, _ = chain.filter.call_args
    assert any("case_id" in str(a) for a in args)
    assert any("status" in str(a) for a in args)


def test_bulk_delete_kills_live_case_runs(tmp_path):
    workdir = tmp_path / "inv-live"
    workdir.mkdir()
    (workdir / "state.json").write_text(
        json.dumps({"status": "executing", "step": 2}), encoding="utf-8"
    )
    live = _Inv("inv-live", "case-1", "executing", workdir=str(workdir))
    hunt = _Inv("inv-hunt", None, "assigned")
    # Fake query does not apply SQL; the hunt is in the result so the
    # Python guard can be shown to leave it running.
    session = _Session(live_rows=[live, hunt], case_count=1)

    deleted = case_records_service.purge_all_cases(session)

    assert deleted == 1
    assert live.status == "failed"
    assert live.master_review_notes == "killed: case reset"
    assert live.case_id == "case-1"
    assert hunt.status == "assigned"
    assert hunt.case_id is None
    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["failure_reason"] == "killed: case reset"
    assert state["step"] == 2
    for q in session.investigation_queries:
        assert q.update_calls == []


def test_bulk_delete_does_not_null_case_id_before_delete():
    session = _Session(live_rows=[], case_count=0)

    case_records_service.purge_all_cases(session)

    for q in session.investigation_queries:
        assert q.update_calls == []


def test_kill_skips_a_hunt_that_leaked_into_the_result():
    hunt = _Inv("inv-hunt", None, "assigned")
    session = _Session(live_rows=[hunt])

    killed = case_records_service.kill_live_case_investigations(session)

    assert killed == []
    assert hunt.status == "assigned"
