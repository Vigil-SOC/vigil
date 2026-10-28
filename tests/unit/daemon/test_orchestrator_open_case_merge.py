"""An alert joins the open Case for its incident (#3292).

The finding already on an open Case merges first (#2245). Then an open Case
``matching_cases`` names takes it, whether or not its run has finished. A
failed read or attach holds the row queued; bookkeeping after the attach never
undoes the merge. A new Case links to the closed Cases it matches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.storage.shared_ioc_repository import CaseMatch
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

FINDING = {
    "finding_id": "f-2",
    "severity": "high",
    "title": "sshd: brute force",
    "entity_context": {"src_ip": "27.102.116.43"},
}
MATCH = CaseMatch("case-1", "ip:27.102.116.43")


@pytest.fixture(autouse=True)
def no_holding(monkeypatch):
    monkeypatch.setattr(
        "services.daemon.orchestrator._open_case_holding", lambda _fid: None
    )


@pytest.fixture
def attach(monkeypatch):
    fake = MagicMock(return_value=True)
    monkeypatch.setattr("services.daemon.orchestrator._attach_finding", fake)
    return fake


@pytest.fixture
def note(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr("services.daemon.orchestrator._note_merge", fake)
    return fake


def _orchestrator(matches) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.matching_cases.return_value = matches
    orch.stats = {"dedup_prevented": 0}
    orch._create_investigation = AsyncMock()
    orch._decide_trigger = MagicMock()
    return orch


@pytest.mark.asyncio
async def test_a_match_attaches_records_and_notes_the_merge(attach, note):
    orch = _orchestrator([MATCH])

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    orch.shared_intel.matching_cases.assert_called_once_with(
        FINDING, open=True, limit=1
    )
    attach.assert_called_once_with("case-1", "f-2")
    orch.shared_intel.record_on_case.assert_called_once_with("case-1", FINDING)
    note.assert_called_once_with(MATCH, FINDING)
    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="entity_on_open_case", merged_into="case-1"
    )
    orch._create_investigation.assert_not_awaited()
    assert orch.stats["dedup_prevented"] == 1


@pytest.mark.asyncio
async def test_no_match_launches(attach, note):
    orch = _orchestrator([])

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    attach.assert_not_called()
    orch._decide_trigger.assert_not_called()
    orch._create_investigation.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_open_case_read_holds_the_row(attach, note):
    orch = _orchestrator([])
    orch.shared_intel.matching_cases.side_effect = RuntimeError("db down")

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    attach.assert_not_called()
    orch._create_investigation.assert_not_awaited()
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_a_failed_attach_holds_the_row(attach, note, caplog):
    attach.return_value = False
    orch = _orchestrator([MATCH])

    with caplog.at_level("WARNING"):
        await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    assert any("leaving queued" in r.message for r in caplog.records)
    orch.shared_intel.record_on_case.assert_not_called()
    note.assert_not_called()
    orch._create_investigation.assert_not_awaited()
    orch._decide_trigger.assert_not_called()
    assert orch.stats["dedup_prevented"] == 0


@pytest.mark.asyncio
async def test_a_failed_note_keeps_the_merge(attach, monkeypatch):
    # _note_merge swallows its own failure; a dead database must not undo the merge.
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("services.daemon.orchestrator.get_db_manager", boom)
    orch = _orchestrator([MATCH])

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="entity_on_open_case", merged_into="case-1"
    )


@pytest.mark.asyncio
async def test_a_finding_on_an_open_case_merges_before_matching(attach, monkeypatch):
    monkeypatch.setattr(
        "services.daemon.orchestrator._open_case_holding", lambda _fid: "case-7"
    )
    orch = _orchestrator([MATCH])

    await orch._create_investigation_for_finding(FINDING, None, trigger_id=9)

    attach.assert_not_called()  # the link already exists
    orch.shared_intel.matching_cases.assert_not_called()
    orch._decide_trigger.assert_called_once_with(
        9, state="merged", reason="finding_on_open_case", merged_into="case-7"
    )


# ---- closed-Case links ----------------------------------------------------


@pytest.fixture
def links(monkeypatch):
    made = []
    monkeypatch.setattr(
        "services.daemon.orchestrator._link_cases",
        lambda a, b, notes: made.append((a, b, notes)),
    )
    return made


@pytest.mark.asyncio
async def test_a_new_case_links_to_the_closed_cases_it_matches(links):
    orch = _orchestrator(
        [
            CaseMatch("old-1", "ip:27.102.116.43"),
            CaseMatch("old-2", "hash:abc"),
            CaseMatch("old-3", "hostname:web-1", same_alert=True),
        ]
    )

    await orch._link_closed_cases("case-new", FINDING)

    orch.shared_intel.matching_cases.assert_called_once_with(
        FINDING, open=False, limit=5, exclude_case_id="case-new"
    )
    assert links == [
        ("case-new", "old-1", "same attacker ip:27.102.116.43"),
        ("case-new", "old-2", "same hash abc"),
        ("case-new", "old-3", "same alert on hostname:web-1"),
    ]


@pytest.mark.asyncio
async def test_a_failed_closed_case_read_or_link_never_raises(links, monkeypatch):
    orch = _orchestrator([])
    orch.shared_intel.matching_cases.side_effect = RuntimeError("db down")
    await orch._link_closed_cases("case-new", FINDING)
    assert links == []

    def boom(*_a):
        raise RuntimeError("link failed")

    monkeypatch.setattr("services.daemon.orchestrator._link_cases", boom)
    orch = _orchestrator([CaseMatch("old-1", "ip:1.2.3.4")])
    await orch._link_closed_cases("case-new", FINDING)
