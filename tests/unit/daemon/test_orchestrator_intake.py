"""Every trigger offered to the orchestrator is a row (#918).

The asyncio.Queue is gone. Producers insert; the intake tick reads queued
rows oldest-first; below-threshold is shed; overlap is merged after attach.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator, lift_ai_enrichment

pytestmark = pytest.mark.unit

HIGH = {
    "finding_id": "f-high",
    "severity": "high",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}
MEDIUM = {
    "finding_id": "f-med",
    "severity": "medium",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}
UNRATED = {
    "finding_id": "f-none",
    "severity": None,
    "entity_context": {"hostnames": ["FYODOR-L"]},
}


def _orchestrator(**extra) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = None
    orch.stats = {"dedup_prevented": 0, "investigations_created": 0}
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._create_manual_investigation = AsyncMock()
    orch._decide_trigger = MagicMock()
    orch._data_service = MagicMock()
    orch._open_case_for_finding = MagicMock(return_value="case-1")
    orch._attach_finding_to_overlap = MagicMock(return_value="case-1")
    for key, value in extra.items():
        setattr(orch, key, value)
    return orch


def test_lift_copies_nested_enrichment_keys_select_workflow_reads():
    finding = {
        "finding_id": "f-1",
        "severity": "high",
        "ai_enrichment": {
            "recommended_action": "isolate",
            "category": "malware",
        },
    }
    lifted = lift_ai_enrichment(finding)
    assert lifted["recommended_action"] == "isolate"
    assert lifted["category"] == "malware"
    # The stored row is unchanged; a second copy would drift.
    assert "recommended_action" not in finding


@pytest.mark.asyncio
async def test_below_threshold_ends_shed_and_opens_nothing():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(MEDIUM, None, trigger_id=7)

    orch._decide_trigger.assert_called_once_with(
        7, state="shed", reason="below_threshold"
    )
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_finding_with_no_severity_stays_queued():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(UNRATED, None, trigger_id=7)

    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_and_human_ask_are_never_shed():
    orch = _orchestrator()
    await orch._process_intake_row(
        {
            "id": 3,
            "kind": "human_ask",
            "priority": "low",
            "payload": {"workflow_id": "threat-hunt"},
        },
        None,
    )
    await orch._process_intake_row(
        {
            "id": 4,
            "kind": "schedule",
            "priority": "low",
            "payload": {"workflow_id": "threat-hunt"},
        },
        None,
    )

    orch._decide_trigger.assert_not_called()
    assert orch._create_manual_investigation.await_count == 2


@pytest.mark.asyncio
async def test_overlap_ends_merged_with_the_case_it_joined():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=9)

    orch._attach_finding_to_overlap.assert_called_once_with("f-high", ["inv-1"])
    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="overlaps_open_work", merged_into="case-1"
    )
    orch._create_investigation.assert_not_awaited()
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_a_high_detection_launches_through_the_existing_path():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=11)

    orch._create_investigation.assert_awaited_once()
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["trigger_id"] == 11
    assert kwargs["priority"] == "high"
    assert kwargs["case_id"] == "case-1"
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_drain_is_oldest_first_and_routes_by_kind():
    orch = _orchestrator()
    orch._queued_intake_triggers = MagicMock(
        return_value=[
            {"id": 1, "kind": "detection", "finding_id": "f-high"},
            {
                "id": 2,
                "kind": "human_ask",
                "priority": "medium",
                "payload": {"workflow_id": "incident-response"},
            },
        ]
    )
    orch._hydrate_detection_finding = MagicMock(return_value=HIGH)

    await orch._drain_intake(None)

    orch._create_investigation.assert_awaited_once()
    orch._create_manual_investigation.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_cas_creates_no_investigation(tmp_path):
    from services.daemon.workdir import WorkdirManager

    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(dry_run=True)
    orch.workdir = WorkdirManager(str(tmp_path))
    orch._workflows = MagicMock()
    orch.shared_intel = MagicMock()
    orch.stats = {"investigations_created": 0}
    orch._save_investigation = MagicMock(return_value=False)
    orch._check_cross_correlations = AsyncMock()

    await orch._create_investigation(
        workflow_id="incident-response",
        findings=[HIGH],
        trigger_type="finding",
        priority="high",
        trigger_id=4,
    )

    orch.shared_intel.register_investigation.assert_not_called()
    assert orch.stats["investigations_created"] == 0


@pytest.mark.asyncio
async def test_processor_inserts_a_detection_row(monkeypatch):
    from services.daemon.config import ProcessingConfig
    from services.daemon.processor import FindingProcessor

    captured = []
    monkeypatch.setattr(
        "services.daemon.orchestrator.insert_intake_trigger",
        lambda **kwargs: captured.append(kwargs) or 1,
    )
    processor = FindingProcessor(ProcessingConfig())
    await processor._evaluate_for_response({"finding_id": "f-1", "severity": "high"})

    assert captured == [{"kind": "detection", "finding_id": "f-1", "priority": "high"}]
    assert processor.stats["queued_for_investigation"] == 1
