"""A finding that overlaps open work attaches to its Case (#921, #1002).

``check_overlap`` names the live investigations sharing an entity. The finding
goes into the first one's Case. Overlap with only caseless runs (hunts) is not
a merge: the row proceeds to Claim. A failed write is not a merge either, and
neither is an overlapping run that will not read: the row stays queued (#997).
``merged_into`` is always a ``cases.case_id``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

FINDING = {
    "finding_id": "f-2",
    "severity": "high",
    "entity_context": {"hostnames": ["FYODOR-L"]},
}


@pytest.fixture
def attach(monkeypatch):
    fake = MagicMock(return_value=True)
    monkeypatch.setattr("services.daemon.orchestrator._attach_finding", fake)
    return fake


def _orchestrator(overlapping, records) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = overlapping
    orch.stats = {"dedup_prevented": 0}
    orch.get_investigation = MagicMock(side_effect=records.get)
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._decide_trigger = MagicMock()
    return orch


def _nothing_new_opened(orch):
    orch._create_investigation.assert_not_awaited()
    assert orch.stats["dedup_prevented"] == 1
    # The old dedup_prevention decision row is gone; only the counter remains.
    orch._log_ai_decision.assert_not_called()


@pytest.mark.asyncio
async def test_attaches_to_the_case_of_the_overlapping_investigation(attach):
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}})

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    attach.assert_called_once_with("case-1", "f-2")
    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="overlaps_open_work", merged_into="case-1"
    )
    _nothing_new_opened(orch)


@pytest.mark.asyncio
async def test_merged_into_is_a_case_id(attach):
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}})

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    assert orch._decide_trigger.call_args.kwargs["merged_into"] == "case-1"


@pytest.mark.asyncio
async def test_prefers_the_first_overlap_that_has_a_case(attach):
    orch = _orchestrator(
        ["inv-1", "inv-2"], {"inv-1": {"case_id": None}, "inv-2": {"case_id": "case-2"}}
    )

    await orch._create_investigation_for_finding(FINDING, None)

    attach.assert_called_once_with("case-2", "f-2")
    _nothing_new_opened(orch)


@pytest.mark.asyncio
async def test_overlap_with_caseless_run_is_not_a_merge(attach):
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": None}})

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    attach.assert_not_called()
    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_awaited_once()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_an_unreadable_overlapping_investigation_holds_the_row(attach, caplog):
    """``check_overlap`` only names live rows, so a read that comes back empty
    is a failed read, not a caseless run. Launching on it would open a second
    run on an entity a Case already covers."""
    orch = _orchestrator(["inv-1"], {})

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    assert any("would not read" in r.message for r in caplog.records)
    attach.assert_not_called()
    orch._create_investigation.assert_not_awaited()
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_a_failed_attach_is_logged_and_does_not_raise(attach, caplog):
    attach.return_value = False
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}})

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None)

    assert any("could not be attached" in r.message for r in caplog.records)
    assert any("leaving queued" in r.message for r in caplog.records)
    orch._create_investigation.assert_not_awaited()
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_a_database_error_reads_as_a_failed_attach(monkeypatch, caplog):
    def boom():
        raise RuntimeError("Database not initialized")

    monkeypatch.setattr("core.storage.service.get_db_manager", boom)
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}})

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None)

    assert any("could not be attached" in r.message for r in caplog.records)
    orch._create_investigation.assert_not_awaited()
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0
