"""The case is minted at Claim, in the launch transaction (#1000).

A finding-run never starts without a case. Hunts (schedule, or a Human Ask
with no findings) stay case-less. A Human Ask that names a case joins it;
one that names findings mints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import CaseSpec, Orchestrator
from services.daemon.plan_generator import _infer_title, select_workflow
from services.daemon.workdir import WorkdirManager

pytestmark = pytest.mark.unit

FINDING = {
    "finding_id": "f-1",
    "severity": "high",
    "description": "three failed logons on FYODOR-L",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}
UNRATED = {
    "finding_id": "f-none",
    "severity": None,
    "description": "three failed logons on FYODOR-L",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}
NOT_A_BAND = {
    "finding_id": "f-foo",
    "severity": "not-a-band",
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
    orch = _orchestrator(data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    workflow_id = select_workflow(FINDING)
    data_service.create_case.assert_not_called()
    kwargs = orch._create_investigation.await_args.kwargs
    spec = kwargs["mint_case"]
    assert spec.title == _infer_title(FINDING, workflow_id)[:200]
    assert spec.finding_ids == ["f-1"]
    assert spec.priority == "high"
    assert kwargs["workflow_id"] == workflow_id
    assert kwargs["priority"] == "high"
    assert kwargs.get("case_id") is None


@pytest.mark.asyncio
async def test_unrated_finding_launches_with_unknown_priority():
    orch = _orchestrator(MagicMock())

    await orch._create_investigation_for_finding(UNRATED, None)

    spec = orch._create_investigation.await_args.kwargs["mint_case"]
    assert spec.priority == "unknown"
    assert orch._create_investigation.await_args.kwargs["priority"] == "unknown"


@pytest.mark.asyncio
async def test_a_severity_name_the_ranker_would_call_unknown_launches_unknown():
    orch = _orchestrator(MagicMock())

    await orch._create_investigation_for_finding(NOT_A_BAND, None)

    spec = orch._create_investigation.await_args.kwargs["mint_case"]
    assert spec.priority == "unknown"
    assert orch._create_investigation.await_args.kwargs["priority"] == "unknown"


@pytest.mark.asyncio
async def test_finding_run_without_a_case_raises():
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()

    with pytest.raises(ValueError, match="needs a case"):
        await orch._create_investigation(
            workflow_id="incident-response",
            findings=[FINDING],
            trigger_type="finding",
            priority="high",
        )


@pytest.mark.asyncio
async def test_human_ask_with_case_id_joins_it():
    data_service = MagicMock()
    data_service.get_finding.return_value = FINDING
    orch = _orchestrator(data_service)

    await orch._create_manual_investigation(
        {
            "workflow_id": "incident-response",
            "case_id": "case-join",
            "finding_ids": ["f-1"],
            "priority": "high",
        },
        None,
        trigger_id=3,
    )

    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] == "case-join"
    assert kwargs["mint_case"] is None


@pytest.mark.asyncio
async def test_human_ask_with_findings_mints():
    data_service = MagicMock()
    data_service.get_finding.return_value = FINDING
    orch = _orchestrator(data_service)

    await orch._create_manual_investigation(
        {
            "workflow_id": "incident-response",
            "finding_ids": ["f-1"],
            "priority": "high",
            "hypothesis": "T1071 on FYODOR-L",
        },
        None,
    )

    kwargs = orch._create_investigation.await_args.kwargs
    spec = kwargs["mint_case"]
    assert spec.finding_ids == ["f-1"]
    assert spec.title == "T1071 on FYODOR-L"
    assert spec.priority == "high"
    assert kwargs["case_id"] is None


@pytest.mark.asyncio
async def test_human_ask_hypothesis_only_is_caseless():
    orch = _orchestrator(MagicMock())

    await orch._create_manual_investigation(
        {
            "workflow_id": "threat-hunt",
            "hypothesis": "T1071 on FYODOR-L",
            "priority": "low",
        },
        None,
    )

    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] is None
    assert kwargs["mint_case"] is None
    assert kwargs["findings"] == []


@pytest.mark.asyncio
async def test_schedule_row_is_caseless():
    orch = _orchestrator(MagicMock())

    await orch._process_intake_row(
        {
            "id": 4,
            "kind": "schedule",
            "priority": "low",
            "payload": {"workflow_id": "threat-hunt"},
        },
        None,
    )

    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] is None
    assert kwargs["mint_case"] is None
    assert kwargs["trigger_type"] == "scheduled"


@pytest.mark.asyncio
async def test_detection_plan_never_says_case_pending(tmp_path):
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(dry_run=True)
    orch.workdir = WorkdirManager(str(tmp_path))
    orch._workflows = MagicMock()
    orch.shared_intel = MagicMock()
    orch.stats = {"investigations_created": 0}
    orch._save_investigation = MagicMock(return_value=True)
    orch._check_cross_correlations = AsyncMock()

    await orch._create_investigation(
        workflow_id="incident-response",
        findings=[FINDING],
        trigger_type="finding",
        priority="high",
        mint_case=CaseSpec(title="t", finding_ids=["f-1"], priority="high"),
    )

    record = orch._save_investigation.call_args[0][0]
    plan = orch.workdir.read_file(record["investigation_id"], "plan.md")
    mint = orch._save_investigation.call_args.kwargs["mint_case"]
    assert "case_id: pending" not in plan
    assert record["case_id"].startswith("case-")
    assert f"case_id: {record['case_id']}" in plan
    assert mint.case_id == record["case_id"]
