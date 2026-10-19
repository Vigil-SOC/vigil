# The account is asked for, not polled: the route forwards read_account and turns
# its two non-answers (None, raise) into the reader's 404 and 502.

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.deps import provide_workflow_runs
from core.storage.schemas.case_account import CaseAccount
from core.workflows import workflows_router as router

pytestmark = pytest.mark.unit

RUN = "wfr-1"
ACCOUNT = CaseAccount.model_validate(
    {
        "schema_version": 1,
        "runs": [{"run_id": RUN, "run_kind": "investigate"}],
        "verdict": {"run_id": RUN, "disposition": "benign", "headline": "A backup."},
    }
)


class _Runs:
    def __init__(self, known):
        self._known = set(known)

    def get_run(self, run_id):
        return {"run_id": run_id} if run_id in self._known else None


def test_the_account_leaves_out_what_the_run_did_not_fill(monkeypatch):
    async def _read(_run_id):
        return ACCOUNT

    monkeypatch.setattr(router, "read_account", _read)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[provide_workflow_runs] = lambda: _Runs([RUN])

    response = TestClient(app).get(f"/workflows/runs/{RUN}/account")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "runs": [{"run_id": RUN, "run_kind": "investigate"}],
        "verdict": {"run_id": RUN, "disposition": "benign", "headline": "A backup."},
    }


@pytest.mark.asyncio
async def test_no_account_is_a_404(monkeypatch):
    async def _read(_run_id):
        return None

    monkeypatch.setattr(router, "read_account", _read)

    with pytest.raises(HTTPException) as raised:
        await router.get_workflow_run_account(RUN, _Runs([RUN]))
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_a_failed_read_is_a_502_without_the_error_text(monkeypatch):
    async def _read(_run_id):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(router, "read_account", _read)

    with pytest.raises(HTTPException) as raised:
        await router.get_workflow_run_account(RUN, _Runs([RUN]))
    assert raised.value.status_code == 502
    assert "secret" not in raised.value.detail


@pytest.mark.asyncio
async def test_a_missing_run_is_a_404_before_serve_is_asked(monkeypatch):
    async def _read(_run_id):
        raise AssertionError("serve must not be asked about an unknown run")

    monkeypatch.setattr(router, "read_account", _read)

    with pytest.raises(HTTPException) as raised:
        await router.get_workflow_run_account("wfr-gone", _Runs([]))
    assert raised.value.status_code == 404
