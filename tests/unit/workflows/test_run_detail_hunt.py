# A hunt writes no phase rows, so the run detail the console expands would say
# "no additional detail" for every hunt. It carries the agent layer's standing instead.

from __future__ import annotations

import pytest

from core.workflows import catalog
from core.workflows import workflows_router as router
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

STANDING = {"status": "active", "iteration": 2, "hypotheses": [], "evidence_count": 0}


def _runs_for(workflow_id: str):
    class _Runs:
        def get_run(self, run_id):
            return {"run_id": run_id, "workflow_id": workflow_id}

        def list_phases(self, _run_id):
            return []

    return _Runs()


async def _reads(_run_id):
    return STANDING


@pytest.mark.asyncio
async def test_a_hunt_run_carries_the_standing_of_its_hypotheses(monkeypatch):
    monkeypatch.setattr(router, "read_projection", _reads)

    detail = await router.get_workflow_run(
        "run-1", _runs_for("threat-hunt"), WorkflowsService()
    )

    assert detail["hunt"] == STANDING


# The four compose definitions are untouched, and asking the agent layer about one
# is a round trip whose answer nothing would read.
@pytest.mark.asyncio
async def test_a_compose_run_is_not_asked_about(monkeypatch):
    asked = []

    async def _record(run_id):
        asked.append(run_id)
        return STANDING

    monkeypatch.setattr(router, "read_projection", _record)

    detail = await router.get_workflow_run(
        "run-2", _runs_for("incident-response"), WorkflowsService()
    )

    assert asked == []
    assert "hunt" not in detail


def test_the_threat_hunt_definition_is_the_one_that_reads_as_a_hunt():
    workflows = WorkflowsService()
    assert catalog.is_hunt(workflows, "threat-hunt")
    assert not catalog.is_hunt(workflows, "incident-response")
    assert not catalog.is_hunt(workflows, None)
