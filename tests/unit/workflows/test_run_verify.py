# Verify is asked for, not polled: the route forwards read_verify and turns its
# two non-answers (None, raise) into the operator's 404 and 502.

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.workflows import workflows_router as router

pytestmark = pytest.mark.unit

RESULT = {"ok": True, "events": 4, "runs": 1}


class _Runs:
    def __init__(self, known):
        self._known = set(known)

    def get_run(self, run_id):
        return {"run_id": run_id} if run_id in self._known else None


@pytest.mark.asyncio
async def test_the_chain_walk_is_returned_as_serve_wrote_it(monkeypatch):
    asked = []

    async def _read(run_id):
        asked.append(run_id)
        return RESULT

    monkeypatch.setattr(router, "read_verify", _read)

    body = await router.verify_workflow_run("wfr-1", _Runs(["wfr-1"]))

    assert body == RESULT
    assert asked == ["wfr-1"]


@pytest.mark.asyncio
async def test_nothing_to_verify_is_a_404(monkeypatch):
    async def _read(_run_id):
        return None

    monkeypatch.setattr(router, "read_verify", _read)

    with pytest.raises(HTTPException) as raised:
        await router.verify_workflow_run("wfr-1", _Runs(["wfr-1"]))
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_a_missing_run_is_a_404_before_serve_is_asked(monkeypatch):
    async def _read(_run_id):
        raise AssertionError("serve must not be asked about an unknown run")

    monkeypatch.setattr(router, "read_verify", _read)

    with pytest.raises(HTTPException) as raised:
        await router.verify_workflow_run("wfr-gone", _Runs([]))
    assert raised.value.status_code == 404
