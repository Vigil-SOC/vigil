# The case's account is asked for, not polled: the route finds the case's runs,
# hands them to the merge, and answers with what it merged and what it could not read.

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.cases.case_account import CaseAccountRead
from core.routing import request_unit_of_work
from core.storage.schemas.case_account import CaseAccount
from services.api.routers import cases as router

pytestmark = pytest.mark.unit

CASE = "case-1"
RUN = "wfr-1"
ACCOUNT = CaseAccount.model_validate(
    {
        "schema_version": 1,
        "runs": [{"run_id": RUN, "run_kind": "investigate"}],
        "verdict": {"run_id": RUN, "disposition": "benign", "headline": "A backup."},
    }
)


def _known(*cases):
    return lambda case_id: {"case_id": case_id} if case_id in cases else None


def test_the_account_and_its_unread_runs_are_answered(monkeypatch):
    asked = {}

    async def _read(run_ids):
        asked["run_ids"] = list(run_ids)
        return CaseAccountRead(account=ACCOUNT, unread=["wfr-2"])

    monkeypatch.setattr(router.data_service, "get_case", _known(CASE))
    monkeypatch.setattr(router, "case_run_ids", lambda _session, _case: [RUN, "wfr-2"])
    monkeypatch.setattr(router, "read_case_account", _read)
    app = FastAPI()
    app.include_router(router.router, prefix="/api/cases")
    app.dependency_overrides[request_unit_of_work] = lambda: None

    response = TestClient(app).get(f"/api/cases/{CASE}/account")

    assert response.status_code == 200
    # What no run filled is left out, never written as null.
    assert response.json() == {
        "account": {
            "schema_version": 1,
            "runs": [{"run_id": RUN, "run_kind": "investigate"}],
            "verdict": {"run_id": RUN, "disposition": "benign", "headline": "A backup."},
        },
        "unread": ["wfr-2"],
    }
    assert asked["run_ids"] == [RUN, "wfr-2"]


@pytest.mark.asyncio
async def test_an_unknown_case_is_a_404_before_any_run_is_read(monkeypatch):
    async def _read(_run_ids):
        raise AssertionError("no run is read for a case that does not exist")

    monkeypatch.setattr(router.data_service, "get_case", _known())
    monkeypatch.setattr(router, "read_case_account", _read)

    with pytest.raises(HTTPException) as raised:
        await router.get_case_account("case-gone", None)
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_a_case_with_no_runs_has_an_empty_account(monkeypatch):
    monkeypatch.setattr(router.data_service, "get_case", _known(CASE))
    monkeypatch.setattr(router, "case_run_ids", lambda _session, _case: [])

    answer = await router.get_case_account(CASE, None)
    assert answer.unread == []
    assert answer.account.runs == []
    assert answer.account.verdict is None
