# One call of a run, opened to check a record against: the route forwards
# read_call's answer, and turns its non-answers into a 404 and a fixed 502.

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.workflows import workflows_router as router

pytestmark = pytest.mark.unit

CALL = {
    "run_id": "wfr-hunt",
    "ref": "r6",
    "tool": "splunk_execute",
    "arguments": '{"spl_query": "index=botsv3 | stats count by dest_ip"}',
    "result": "25 row(s) from splunk",
}


class _Runs:
    def __init__(self, known):
        self._known = set(known)

    def get_run(self, run_id):
        return {"run_id": run_id} if run_id in self._known else None


@pytest.mark.asyncio
async def test_the_call_is_returned_as_serve_journaled_it(monkeypatch):
    async def _read(run_id, ref):
        assert (run_id, ref) == ("wfr-hunt", "r6")
        return CALL

    monkeypatch.setattr(router, "read_call", _read)
    assert await router.read_workflow_run_call("wfr-hunt", "r6", _Runs(["wfr-hunt"])) == CALL


@pytest.mark.asyncio
async def test_a_ref_the_run_never_made_is_a_404(monkeypatch):
    async def _read(run_id, ref):
        return None

    monkeypatch.setattr(router, "read_call", _read)
    with pytest.raises(HTTPException) as raised:
        await router.read_workflow_run_call("wfr-hunt", "r99", _Runs(["wfr-hunt"]))
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_an_agent_layer_failure_is_a_502_without_its_text(monkeypatch):
    async def _read(run_id, ref):
        raise RuntimeError("the agent layer answered 500: postgres://user@host")

    monkeypatch.setattr(router, "read_call", _read)
    with pytest.raises(HTTPException) as raised:
        await router.read_workflow_run_call("wfr-hunt", "r6", _Runs(["wfr-hunt"]))
    assert raised.value.status_code == 502
    assert "postgres" not in raised.value.detail


@pytest.mark.asyncio
async def test_a_missing_run_is_a_404_before_serve_is_asked(monkeypatch):
    async def _read(run_id, ref):
        raise AssertionError("serve must not be asked about an unknown run")

    monkeypatch.setattr(router, "read_call", _read)
    with pytest.raises(HTTPException) as raised:
        await router.read_workflow_run_call("wfr-gone", "r1", _Runs([]))
    assert raised.value.status_code == 404
