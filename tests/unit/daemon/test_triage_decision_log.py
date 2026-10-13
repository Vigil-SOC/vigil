"""An answered daemon triage leaves one AI decision row an analyst can grade."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.llm import outage
from core.storage.models import AIDecisionLog
from services.daemon.config import ProcessingConfig
from services.daemon.probes import PROBE_DATA_SOURCE
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit

REPLY = (
    "SEVERITY: high\nCONFIDENCE: 0.82\nCATEGORY: Malware\n"
    "RECOMMENDED_ACTION: isolate\nREASONING: beaconing to a known C2"
)


@pytest.fixture(autouse=True)
def _clean_outages():
    outage._failures.clear()
    yield
    outage._failures.clear()


class _Db:
    """Stands in for get_db_manager(): keeps what each session added, and
    whether the processor still held its LLM slot while it wrote."""

    def __init__(self, processor=None):
        self.added = []
        self.slot_held = []
        self._processor = processor

    @contextmanager
    def session_scope(self):
        if self._processor is not None:
            self.slot_held.append(self._processor._semaphore.locked())
        session = Mock()
        session.add.side_effect = self.added.append
        yield session


def _processor(reply: str = REPLY, stored: bool = True) -> FindingProcessor:
    processor = FindingProcessor(
        ProcessingConfig(
            auto_triage_enabled=True,
            auto_enrich_enabled=False,
            max_concurrent_tasks=1,
        )
    )
    gateway = Mock()
    gateway.submit = AsyncMock(return_value={"content": reply})
    processor._llm_gateway = gateway
    # update_finding is False when no row took the update: a finding ingest
    # skipped as an external-id duplicate (#1935).
    processor._data_service = Mock()
    processor._data_service.update_finding.return_value = stored
    return processor


def _enrich(processor, finding, db, target=("prov-1", "model-x")):
    with patch.object(
        FindingProcessor, "_resolve_triage_target", return_value=target
    ), patch.object(FindingProcessor, "_evaluate_for_response", AsyncMock()), patch(
        "core.storage.connection.get_db_manager", return_value=db
    ):
        asyncio.run(processor._enrich_in_background(finding))


def test_an_answered_triage_logs_its_verdict_under_the_triage_filter():
    processor = _processor()
    db = _Db(processor)
    _enrich(processor, {"finding_id": "f-1", "data_source": "x"}, db)

    [row] = db.added
    assert isinstance(row, AIDecisionLog)
    assert row.decision_id.startswith("triage-")
    # The AI Decisions agent filter lists the registry's decision ids.
    triage_agent = next(r for r in BUILTIN_AGENTS if r["id"] == "triage")
    assert row.agent_id == triage_agent["decision_id"]
    assert (row.decision_type, row.finding_id) == ("triage", "f-1")
    assert (row.case_id, row.workflow_id) == (None, None)
    assert row.confidence_score == 0.82
    assert row.recommended_action == "isolate"
    assert row.reasoning == "beaconing to a known C2"
    assert row.decision_metadata == {
        "source": "auto_triage",
        "severity": "high",
        "category": "malware",
        "provider_id": "prov-1",
        "model": "model-x",
    }
    # Written after the LLM slot was given back.
    assert db.slot_held == [False]


def test_a_reply_without_a_confidence_is_logged_without_one():
    db = _Db()
    _enrich(
        _processor("SEVERITY: low\nRECOMMENDED_ACTION: monitor"),
        {"finding_id": "f-1"},
        db,
    )
    [row] = db.added
    assert (row.confidence_score, row.reasoning) == (None, "")
    assert row.decision_metadata["category"] is None


def test_a_failed_write_is_logged_once_and_triage_still_succeeds(caplog):
    broken = Mock()
    broken.session_scope.side_effect = RuntimeError("db down")
    processor = _processor()
    with caplog.at_level(logging.INFO, logger="services.daemon.processor"):
        for n in range(3):
            _enrich(processor, {"finding_id": f"f-{n}"}, broken)
        _enrich(processor, {"finding_id": "f-3"}, _Db())

    assert processor.stats["triaged"] == 4
    assert processor.stats["errors"] == 0
    # The verdict still reached the finding row each time.
    for call in processor._data_service.update_finding.call_args_list:
        assert call.kwargs["ai_enrichment"]["recommended_action"] == "isolate"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "f-0" in errors[0].getMessage()
    assert any("recovered after 3" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "target,reply",
    [(None, REPLY), (("prov-1", "model-x"), "")],
    ids=["no-provider", "empty-reply"],
)
def test_no_row_when_triage_got_no_answer(target, reply):
    db = Mock()
    _enrich(_processor(reply), {"finding_id": "f-1"}, db, target=target)
    db.session_scope.assert_not_called()


@pytest.mark.parametrize(
    "finding,reply,stored",
    [
        ({"finding_id": "f-1", "data_source": PROBE_DATA_SOURCE}, REPLY, True),
        ({"finding_id": "f-1"}, "I can't assess this alert.", True),
        ({"finding_id": "f-1"}, REPLY, False),
    ],
    ids=["probe", "no-verdict", "row-not-updated"],
)
def test_no_row_for_a_probe_a_reply_with_no_verdict_or_a_finding_with_no_row(
    finding, reply, stored
):
    db = Mock()
    processor = _processor(reply, stored=stored)
    _enrich(processor, finding, db)
    assert processor.stats["triaged"] == 1
    db.session_scope.assert_not_called()


def test_a_finding_queued_or_in_flight_is_not_spawned_again():
    """The backfill reads every row still missing ai_enrichment, including the
    ones the ingest path has already queued or is triaging."""
    processor = FindingProcessor(ProcessingConfig(enrich_max_inflight=1))
    started = []

    async def run():
        release = asyncio.Event()

        async def enrich(finding, source=None):
            started.append(finding["finding_id"])
            await release.wait()

        with patch.object(processor, "_enrich_in_background", enrich):
            await processor._spawn_enrich({"finding_id": "f-1"})
            # f-2 waits for the one slot f-1 holds.
            waiting = asyncio.create_task(
                processor._spawn_enrich({"finding_id": "f-2"})
            )
            await asyncio.sleep(0)
            # The backfill offers both again: one in flight, one queued. Each
            # returns at once; a spawn would wait for the slot instead.
            for finding_id in ("f-1", "f-2"):
                await asyncio.wait_for(
                    processor._spawn_enrich({"finding_id": finding_id}), timeout=1
                )

            release.set()
            await waiting
            while processor._enrich_tasks:
                await asyncio.gather(*processor._enrich_tasks)
                await asyncio.sleep(0)
            assert started == ["f-1", "f-2"]

            # Finished, so a later sweep may take it again.
            await processor._spawn_enrich({"finding_id": "f-1"})
            await asyncio.gather(*processor._enrich_tasks)
            await asyncio.sleep(0)

    asyncio.run(run())
    assert started == ["f-1", "f-2", "f-1"]
    assert processor._enrich_inflight == set()
