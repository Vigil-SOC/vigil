# POST /workflows/threat-hunt/coverage is the same function as the agent tool,
# and it never reaches execute_workflow (#903).

from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.memory import hunt_coverage
from core.workflows import workflows_router as router
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

IP = "ip:203.0.113.7"


@pytest.fixture
def prior_hunts(monkeypatch):
    monkeypatch.setattr(
        hunt_coverage,
        "list_prior_hunts",
        lambda keys, techniques=(): {
            "keys": list(keys),
            "techniques": list(techniques),
            "concluded": [],
            "in_flight": [],
        },
    )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the coverage route must not execute a workflow")

    monkeypatch.setattr(WorkflowsService, "execute_workflow", _forbidden)


@pytest.mark.asyncio
async def test_plain_text_report_is_uncovered_with_a_proposal(prior_hunts):
    payload = router.HuntCoverageRequest(
        report="Phishing infrastructure at 203.0.113.7 delivering T1566 lures"
    )

    result = await router.check_hunt_coverage(payload)

    assert result["status"] == "uncovered"
    assert result["keys"] == [IP]
    assert result["techniques"] == ["T1566"]
    assert result["proposal"]["hypothesis_subjects"] == {
        result["proposal"]["hypothesis"]: [IP]
    }


@pytest.mark.asyncio
async def test_an_empty_ask_is_a_400(prior_hunts):
    with pytest.raises(HTTPException) as refused:
        await router.check_hunt_coverage(router.HuntCoverageRequest(report=""))

    assert refused.value.status_code == 400
