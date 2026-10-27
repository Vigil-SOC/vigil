"""NetFlow repeats of one host pair join that pair's open Case.

Matched on the pair, not a single IP; a finding above every severity on the
Case, with no run live on it, re-runs on that Case instead of minting one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.pair_case_repository import PairCase
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import (
    Orchestrator,
    _netflow_pair,
    _raises_severity,
)

pytestmark = pytest.mark.unit


def _netflow(severity: str = "high") -> dict:
    return {
        "finding_id": "f-nf",
        "data_source": "loglm",
        "severity": severity,
        "entity_context": {
            "src_ip": "10.0.0.5",
            "dst_ip": "192.168.10.84",
            "source_evidence": {"telemetry_kind": "netflow"},
        },
    }


def _orchestrator(pair_case=(True, None)) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = None
    orch.stats = {"dedup_prevented": 0, "investigations_created": 0}
    orch._log_ai_decision = MagicMock()
    orch._create_investigation = AsyncMock()
    orch._decide_trigger = MagicMock()
    orch._data_service = MagicMock()
    orch._data_service.add_finding_to_case.return_value = True
    orch._pair_case = MagicMock(return_value=pair_case)
    return orch


def _case(*severities: str, live: bool = False) -> PairCase:
    return PairCase("case-pair", tuple(severities), live)


def test_pair_is_only_read_from_netflow_findings():
    assert _netflow_pair(_netflow()) == ("10.0.0.5", "192.168.10.84")
    generic = {
        "src_ip": "10.0.0.5",
        "dst_ip": "192.168.10.84",
        "source_evidence": {"telemetry_kind": "generic_log"},
    }
    assert _netflow_pair({**_netflow(), "entity_context": generic}) is None
    assert (
        _netflow_pair({**_netflow(), "entity_context": {"src_ip": "1.2.3.4"}}) is None
    )


def test_raises_severity_compares_against_the_most_severe_on_the_case():
    assert _raises_severity("critical", ("high", "medium"))
    assert not _raises_severity("high", ("high", "low"))
    assert not _raises_severity("medium", ("critical",))
    assert not _raises_severity(None, ("low",))


@pytest.mark.asyncio
async def test_new_pair_opens_its_own_case_without_ip_overlap():
    orch = _orchestrator()

    await orch._create_investigation_for_finding(_netflow(), None, trigger_id=7)

    orch.shared_intel.check_overlap.assert_not_called()
    orch._decide_trigger.assert_not_called()
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] is None
    assert kwargs["mint_case"].finding_ids == ["f-nf"]


@pytest.mark.asyncio
async def test_repeat_of_pair_joins_its_open_case():
    orch = _orchestrator(pair_case=(True, _case("high", "medium")))

    await orch._create_investigation_for_finding(_netflow("high"), None, trigger_id=7)

    orch._data_service.add_finding_to_case.assert_called_once_with("case-pair", "f-nf")
    orch._decide_trigger.assert_called_once_with(
        7, state="merged", reason="same_pair_open_case", merged_into="case-pair"
    )
    orch._create_investigation.assert_not_awaited()
    assert orch.stats["dedup_prevented"] == 1


@pytest.mark.asyncio
async def test_more_severe_repeat_reruns_on_the_same_case():
    orch = _orchestrator(pair_case=(True, _case("high", "medium")))

    await orch._create_investigation_for_finding(
        _netflow("critical"), None, trigger_id=7
    )

    orch._decide_trigger.assert_not_called()
    orch._data_service.add_finding_to_case.assert_called_once_with("case-pair", "f-nf")
    kwargs = orch._create_investigation.await_args.kwargs
    assert kwargs["case_id"] == "case-pair"
    assert kwargs["mint_case"] is None
    assert kwargs["trigger_id"] == 7


@pytest.mark.asyncio
async def test_more_severe_repeat_joins_when_a_run_is_already_live():
    orch = _orchestrator(pair_case=(True, _case("medium", live=True)))

    await orch._create_investigation_for_finding(
        _netflow("critical"), None, trigger_id=7
    )

    orch._decide_trigger.assert_called_once_with(
        7, state="merged", reason="same_pair_open_case", merged_into="case-pair"
    )
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_lookup_leaves_row_queued():
    orch = _orchestrator(pair_case=(False, None))

    await orch._create_investigation_for_finding(_netflow(), None, trigger_id=7)

    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_attach_leaves_row_queued():
    orch = _orchestrator(pair_case=(True, _case("high")))
    orch._data_service.add_finding_to_case.return_value = False

    await orch._create_investigation_for_finding(_netflow("high"), None, trigger_id=7)

    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_not_awaited()
    assert orch.stats["dedup_prevented"] == 0


def test_repeat_on_resolve_is_merged_not_launchable():
    orch = _orchestrator(pair_case=(True, _case("high")))
    orch._hydrate_detection_finding = MagicMock(return_value=_netflow("medium"))
    row = {"id": 7, "kind": "detection", "finding_id": "f-nf"}

    from core.time import utcnow

    assert orch._resolve_intake_row(row, utcnow()) is None
    orch._decide_trigger.assert_called_once()
