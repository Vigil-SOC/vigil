# Replay is asked for, not polled: the route forwards read_replay's answer and
# turns its two non-answers (None, raise) into the operator's 404 and 502.

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.workflows import workflows_router as router

pytestmark = pytest.mark.unit

REPORT = {
    "hunt_id": "wfr-hunt",
    "decisions": [
        {"decision_id": "dec-1", "rebuilt": {}, "recorded": {}, "mismatch": None}
    ],
    "recalled": ["192.0.2.10 is a scheduled backup target"],
}


class _Runs:
    def __init__(self, known):
        self._known = set(known)

    def get_run(self, run_id):
        return {"run_id": run_id} if run_id in self._known else None


@pytest.mark.asyncio
async def test_the_report_is_returned_as_serve_wrote_it(monkeypatch):
    asked = []

    async def _read(run_id, decision_id=None):
        asked.append((run_id, decision_id))
        return REPORT

    monkeypatch.setattr(router, "read_replay", _read)

    body = await router.replay_workflow_run("wfr-hunt", "dec-1", _Runs(["wfr-hunt"]))

    assert body == REPORT
    assert asked == [("wfr-hunt", "dec-1")]


@pytest.mark.asyncio
async def test_nothing_to_replay_is_a_404(monkeypatch):
    async def _read(run_id, decision_id=None):
        return None

    monkeypatch.setattr(router, "read_replay", _read)

    with pytest.raises(HTTPException) as raised:
        await router.replay_workflow_run("wfr-compose", None, _Runs(["wfr-compose"]))
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_an_agent_layer_failure_is_a_502_with_the_reason(monkeypatch):
    async def _read(run_id, decision_id=None):
        raise RuntimeError("the agent layer answered 502: ledger would not fold")

    monkeypatch.setattr(router, "read_replay", _read)

    with pytest.raises(HTTPException) as raised:
        await router.replay_workflow_run("wfr-hunt", None, _Runs(["wfr-hunt"]))
    assert raised.value.status_code == 502
    assert "would not fold" in raised.value.detail


@pytest.mark.asyncio
async def test_a_missing_run_is_a_404_before_serve_is_asked(monkeypatch):
    async def _read(run_id, decision_id=None):
        raise AssertionError("serve must not be asked about an unknown run")

    monkeypatch.setattr(router, "read_replay", _read)

    with pytest.raises(HTTPException) as raised:
        await router.replay_workflow_run("wfr-gone", None, _Runs([]))
    assert raised.value.status_code == 404
