"""An admitted finding opens its case at admission (#920).

The coalescing sibling needs an open case to attach evidence to, so the case is
minted before the investigation, and the investigation launches under it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator
from services.daemon.plan_generator import _infer_title, select_workflow

pytestmark = pytest.mark.unit

FINDING = {
    "finding_id": "f-1",
    "severity": "high",
    "description": "three failed logons on FYODOR-L",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}


def _orchestrator(data_service) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = None
    orch.stats = {"dedup_prevented": 0}
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._data_service = data_service
    return orch


@pytest.mark.asyncio
async def test_opens_a_case_and_launches_under_it():
    data_service = MagicMock()
    data_service.create_case.return_value = {"case_id": "case-1", "title": "x"}
    orch = _orchestrator(data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    workflow_id = select_workflow(FINDING)
    # One title rule: the case is titled the way the plan is.
    data_service.create_case.assert_called_once_with(
        _infer_title(FINDING, workflow_id), ["f-1"], priority="high"
    )
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] == "case-1"
    assert kwargs["workflow_id"] == workflow_id
    assert kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_a_failed_case_create_still_launches_without_one():
    data_service = MagicMock()
    data_service.create_case.return_value = None
    orch = _orchestrator(data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    assert orch._create_investigation.await_args.kwargs["case_id"] is None


@pytest.mark.asyncio
async def test_no_data_service_still_launches_without_a_case():
    orch = _orchestrator(None)

    await orch._create_investigation_for_finding(FINDING, None)

    assert orch._create_investigation.await_args.kwargs["case_id"] is None
