"""A finding that overlaps open work attaches to it instead of being dropped (#921).

``check_overlap`` names the live investigations sharing an entity. The finding
goes into the first one's case; when none has a case, onto its ``trigger_ids``.
Either way nothing new is opened.
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


def _orchestrator(overlapping, records, data_service) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = overlapping
    orch.stats = {"dedup_prevented": 0}
    orch.get_investigation = MagicMock(side_effect=records.get)
    orch._append_trigger_id = MagicMock()
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._data_service = data_service
    return orch


def _nothing_new_opened(orch, data_service):
    orch._create_investigation.assert_not_awaited()
    if data_service is not None:
        data_service.create_case.assert_not_called()
    assert orch.stats["dedup_prevented"] == 1
    # The old dedup_prevention decision row is gone; only the counter remains.
    assert not [
        c
        for c in orch._log_ai_decision.call_args_list
        if c.kwargs.get("decision_type") == "dedup_prevention"
    ]


@pytest.mark.asyncio
async def test_attaches_to_the_case_of_the_overlapping_investigation():
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = True
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    data_service.add_finding_to_case.assert_called_once_with("case-1", "f-2")
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_prefers_the_first_overlap_that_has_a_case():
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = True
    orch = _orchestrator(
        ["inv-1", "inv-2"],
        {"inv-1": {"case_id": None}, "inv-2": {"case_id": "case-2"}},
        data_service,
    )

    await orch._create_investigation_for_finding(FINDING, None)

    data_service.add_finding_to_case.assert_called_once_with("case-2", "f-2")
    orch._append_trigger_id.assert_not_called()


@pytest.mark.asyncio
async def test_without_a_case_the_finding_goes_onto_trigger_ids():
    data_service = MagicMock()
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": None}}, data_service)

    await orch._create_investigation_for_finding(FINDING, None)

    orch._append_trigger_id.assert_called_once_with("inv-1", "f-2")
    data_service.add_finding_to_case.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_a_failed_attach_is_logged_and_does_not_raise(caplog):
    data_service = MagicMock()
    data_service.add_finding_to_case.return_value = False
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, data_service)

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None)

    assert any("could not be attached" in r.message for r in caplog.records)
    # Not retried on the trigger_ids path either: the case exists, the write failed.
    orch._append_trigger_id.assert_not_called()
    _nothing_new_opened(orch, data_service)


@pytest.mark.asyncio
async def test_no_data_service_reads_as_a_failed_attach():
    orch = _orchestrator(["inv-1"], {"inv-1": {"case_id": "case-1"}}, None)

    await orch._create_investigation_for_finding(FINDING, None)

    _nothing_new_opened(orch, None)
