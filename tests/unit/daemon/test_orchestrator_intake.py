"""Every trigger offered to the orchestrator is a row (#918).

Producers insert; the intake tick reads queued
rows; rated findings launch or merge; overlap is merged after attach.
A failed attach leaves the row queued (#997).
Ranking, TTL and slot-wait live in test_orchestrator_rank.py (#922).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import (
    Orchestrator,
    _Overlap,
    lift_ai_enrichment,
)

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
    orch._attach_to_overlapping_case = MagicMock(
        return_value=(_Overlap.MERGED, "case-1")
    )
    orch._in_flight = MagicMock(return_value=0)
    orch._queued_intake_depth = MagicMock(return_value=0)
    orch._intake_surge_active = False
    for key, value in extra.items():
        setattr(orch, key, value)
    return orch


def test_merged_into_is_as_wide_as_a_case():
    from core.storage.models import Case, IntakeTrigger

    assert (
        IntakeTrigger.__table__.c.merged_into.type.length
        >= Case.__table__.c.case_id.type.length
    )


def test_intake_case_id_is_as_wide_as_a_case():
    from core.storage.models import Case, IntakeTrigger

    assert (
        IntakeTrigger.__table__.c.case_id.type.length
        == Case.__table__.c.case_id.type.length
    )


def test_investigation_status_defaults_to_assigned():
    from core.storage.models import Investigation

    assert Investigation.__table__.c.status.default.arg == "assigned"


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
async def test_a_medium_finding_launches_and_is_not_shed():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(MEDIUM, None, trigger_id=7)

    orch._create_investigation.assert_awaited_once()
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["trigger_id"] == 7
    assert kwargs["priority"] == "medium"
    orch._decide_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_an_unrated_finding_launches_when_asked():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(UNRATED, None, trigger_id=7)

    orch._create_investigation.assert_awaited_once()
    assert orch._create_investigation.await_args.kwargs["priority"] == "unknown"
    orch._decide_trigger.assert_not_called()


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

    orch._attach_to_overlapping_case.assert_called_once_with("f-high", ["inv-1"])
    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="overlaps_open_work", merged_into="case-1"
    )
    orch._create_investigation.assert_not_awaited()
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_a_medium_finding_that_overlaps_merges_instead_of_shedding():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]

    await orch._create_investigation_for_finding(MEDIUM, None, trigger_id=8)

    orch._attach_to_overlapping_case.assert_called_once_with("f-med", ["inv-1"])
    orch._decide_trigger.assert_called_once_with(
        8, state="merged", reason="overlaps_open_work", merged_into="case-1"
    )
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_attach_leaves_row_queued():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]
    orch._attach_to_overlapping_case = MagicMock(return_value=(_Overlap.HOLD, None))

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=9)

    orch._attach_to_overlapping_case.assert_called_once_with("f-high", ["inv-1"])
    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_attach_does_not_count_dedup():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]
    orch._attach_to_overlapping_case = MagicMock(return_value=(_Overlap.HOLD, None))

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=9)

    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_failed_attach_on_resolve_is_not_launchable():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]
    orch._attach_to_overlapping_case = MagicMock(return_value=(_Overlap.HOLD, None))
    orch._hydrate_detection_finding = MagicMock(return_value=HIGH)
    row = {"id": 9, "kind": "detection", "finding_id": "f-high"}

    kept = orch._resolve_intake_row(row, utcnow())

    assert kept is None
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_overlap_with_caseless_run_is_not_a_merge():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-hunt"]
    orch._attach_to_overlapping_case = MagicMock(return_value=(_Overlap.LAUNCH, None))

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=9)

    orch._attach_to_overlapping_case.assert_called_once_with("f-high", ["inv-hunt"])
    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_awaited_once()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_caseless_overlap_on_resolve_stays_launchable():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-hunt"]
    orch._attach_to_overlapping_case = MagicMock(return_value=(_Overlap.LAUNCH, None))
    orch._hydrate_detection_finding = MagicMock(return_value=HIGH)
    row = {"id": 9, "kind": "detection", "finding_id": "f-high"}

    kept = orch._resolve_intake_row(row, utcnow())

    assert kept is row
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_a_high_detection_launches_through_the_existing_path():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(HIGH, None, trigger_id=11)

    orch._create_investigation.assert_awaited_once()
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["trigger_id"] == 11
    assert kwargs["priority"] == "high"
    assert kwargs["mint_case"].finding_ids == ["f-high"]
    assert kwargs["mint_case"].priority == "high"
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_drain_routes_detection_and_human_ask():
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
        case_id="case-1",
        trigger_id=4,
    )

    orch.shared_intel.register_investigation.assert_not_called()
    assert orch.stats["investigations_created"] == 0


@pytest.mark.asyncio
async def test_post_investigations_inserts_a_human_ask_the_tick_launches(
    monkeypatch,
):
    from services.api.routers.orchestrator import (
        InvestigationCreateRequest,
        create_investigation,
    )

    captured = []
    monkeypatch.setattr(
        "services.daemon.orchestrator.insert_intake_trigger",
        lambda **kwargs: captured.append(kwargs) or 1,
    )

    result = await create_investigation(
        InvestigationCreateRequest(
            workflow_id="threat-hunt",
            hypothesis="T1071 on FYODOR-L",
            priority="low",
        )
    )

    assert result["success"] is True
    assert captured == [
        {
            "kind": "human_ask",
            "priority": "low",
            "payload": {
                "workflow_id": "threat-hunt",
                "finding_ids": [],
                "case_id": None,
                "hypothesis": "T1071 on FYODOR-L",
                "hypothesis_subjects": None,
            },
        }
    ]

    orch = _orchestrator()
    orch._queued_intake_triggers = MagicMock(
        return_value=[
            {
                "id": 1,
                "kind": captured[0]["kind"],
                "priority": captured[0]["priority"],
                "payload": captured[0]["payload"],
            }
        ]
    )

    await orch._drain_intake(None)

    orch._create_manual_investigation.assert_awaited_once()
    item = orch._create_manual_investigation.await_args.args[0]
    assert item["workflow_id"] == "threat-hunt"
    assert item["hypothesis"] == "T1071 on FYODOR-L"
    assert item["priority"] == "low"
    assert orch._create_manual_investigation.await_args.kwargs["trigger_id"] == 1


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


def _scan_session(investigations, findings):
    class Session:
        def query(self, model):
            self.model = model
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def all(self):
            if getattr(self.model, "__name__", "") == "Investigation":
                return investigations
            return findings

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Session()


@pytest.mark.asyncio
async def test_scan_findings_inserts_detection_rows(monkeypatch):
    from types import SimpleNamespace

    from services.api.routers.orchestrator import (
        ScanFindingsRequest,
        scan_existing_findings,
    )

    captured = []
    monkeypatch.setattr(
        "services.daemon.orchestrator.insert_intake_trigger",
        lambda **kwargs: captured.append(kwargs) or 1,
    )

    inv = SimpleNamespace(trigger_ids=["f-done"])
    finding_new = SimpleNamespace(finding_id="f-new", severity="high")
    finding_done = SimpleNamespace(finding_id="f-done", severity="critical")

    db = MagicMock()
    db.session_scope.return_value = _scan_session([inv], [finding_new, finding_done])
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    result = await scan_existing_findings(
        ScanFindingsRequest(severities=["critical", "high"])
    )

    assert result["queued"] == 1
    assert result["skipped_already_investigated"] == 1
    assert captured == [
        {
            "kind": "detection",
            "priority": "high",
            "finding_id": "f-new",
            "payload": {"trigger_type": "scan"},
        }
    ]


@pytest.mark.asyncio
async def test_second_scan_of_a_queued_finding_is_a_noop(monkeypatch):
    from types import SimpleNamespace

    from services.api.routers.orchestrator import (
        ScanFindingsRequest,
        scan_existing_findings,
    )

    captured = []

    def insert(**kwargs):
        captured.append(kwargs)
        return None if len(captured) > 1 else 1

    monkeypatch.setattr("services.daemon.orchestrator.insert_intake_trigger", insert)

    finding = SimpleNamespace(finding_id="f-new", severity="high")
    db = MagicMock()
    db.session_scope.return_value = _scan_session([], [finding])
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    first = await scan_existing_findings(ScanFindingsRequest())
    second = await scan_existing_findings(ScanFindingsRequest())

    assert first["queued"] == 1
    assert second["queued"] == 0
    assert len(captured) == 2
    assert all(call["kind"] == "detection" for call in captured)
    assert all(call["finding_id"] == "f-new" for call in captured)


@pytest.mark.asyncio
async def test_scan_row_merges_into_live_case():
    orch = _orchestrator()
    orch.shared_intel.check_overlap.return_value = ["inv-1"]
    orch._hydrate_detection_finding = MagicMock(return_value=HIGH)
    row = {
        "id": 12,
        "kind": "detection",
        "finding_id": "f-high",
        "payload": {"trigger_type": "scan"},
    }

    kept = orch._resolve_intake_row(row, utcnow())

    assert kept is None
    orch._attach_to_overlapping_case.assert_called_once_with("f-high", ["inv-1"])
    orch._decide_trigger.assert_called_once_with(
        12, state="merged", reason="overlaps_open_work", merged_into="case-1"
    )
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_records_queued_depth_after_expire_merge_and_launch():
    orch = _orchestrator()
    orch._queued_intake_triggers = MagicMock(return_value=[])
    orch._queued_intake_depth = MagicMock(return_value=7)
    orch._record_intake_depth = MagicMock()

    await orch._drain_intake(None)

    orch._queued_intake_depth.assert_called_once()
    orch._record_intake_depth.assert_called_once_with(7)


def test_crossing_depth_notifies_once_until_it_falls_and_re_crosses():
    orch = _orchestrator()
    orch.config.intake_surge_depth = 3
    orch._write_intake_surge_notification = MagicMock()

    orch._record_intake_depth(3)
    orch._write_intake_surge_notification.assert_not_called()

    orch._record_intake_depth(4)
    orch._write_intake_surge_notification.assert_called_once_with(4)

    orch._record_intake_depth(9)
    orch._write_intake_surge_notification.assert_called_once_with(4)

    orch._record_intake_depth(3)
    orch._write_intake_surge_notification.assert_called_once_with(4)

    orch._record_intake_depth(4)
    assert orch._write_intake_surge_notification.call_args_list == [
        ((4,),),
        ((4,),),
    ]


def test_intake_surge_notification_is_caseless_and_carries_depth(monkeypatch):
    from core.storage.models import CaseNotification

    added = []

    class Session:
        def add(self, row):
            added.append(row)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    db = MagicMock()
    db.session_scope.return_value = Session()
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    orch = _orchestrator()
    orch._write_intake_surge_notification(12)

    assert len(added) == 1
    notif = added[0]
    assert isinstance(notif, CaseNotification)
    assert notif.case_id is None
    assert notif.notification_type == "intake_surge"
    assert notif.notification_metadata == {"queue_depth": 12}
    assert "12" in notif.message
