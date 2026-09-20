# Three known-answer probes go through store → triage once a day and stop there
# (#923): never the responder, never the orchestrator.

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from core.memory.source_tier import InvestigationKind, SourceTier, resolve_source_tier
from core.time import utcnow
from services.daemon.config import ProcessingConfig, SchedulerConfig
from services.daemon.probes import (
    ACTIONS,
    PROBE_DATA_SOURCE,
    PROBES,
    SEVERITIES,
    build_probe_finding,
    inject_probes,
    probe_finding_id,
)
from services.daemon.processor import FindingProcessor
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit


class _Data:
    def __init__(self, existing=()):
        self.existing = set(existing)

    def get_finding(self, finding_id):
        return {"finding_id": finding_id} if finding_id in self.existing else None


class TestTheProbeDefinitions:
    def test_three_probes_with_ids_that_fit_the_column(self):
        assert len(PROBES) == 3
        for probe in PROBES:
            assert len(probe_finding_id(probe["name"], date(2026, 12, 31))) <= 50

    def test_carry_no_hashes_or_mitre_and_a_known_answer_in_the_vocabulary(self):
        for probe in PROBES:
            assert "file_hashes" not in probe["entity_context"]
            assert "mitre_predictions" not in probe
            expected = probe["expected"]
            assert expected["severity"] and set(expected["severity"]) <= set(SEVERITIES)
            assert expected["recommended_action"]
            assert set(expected["recommended_action"]) <= set(ACTIONS)

    # A set of probes that all expect the same answer cannot tell a stuck
    # triage from a working one.
    def test_expected_answers_differ(self):
        answers = {
            (
                tuple(p["expected"]["severity"]),
                tuple(p["expected"]["recommended_action"]),
            )
            for p in PROBES
        }
        assert len(answers) > 1

    def test_the_finding_is_a_probe_with_its_answer_under_entity_context(self):
        finding = build_probe_finding(PROBES[0], date(2026, 9, 20))
        assert finding["finding_id"] == f"probe:{PROBES[0]['name']}:2026-09-20"
        assert finding["data_source"] == PROBE_DATA_SOURCE
        assert finding["timestamp"]
        assert finding["entity_context"]["probe"] == {
            "name": PROBES[0]["name"],
            "expected": PROBES[0]["expected"],
        }


class TestTheSweep:
    async def test_puts_three_findings_on_the_processor_queue(self):
        queue: asyncio.Queue = asyncio.Queue()

        injected = await inject_probes(queue, _Data())

        assert injected == 3 and queue.qsize() == 3
        item = queue.get_nowait()
        assert item["type"] == "finding"
        assert item["source"] == PROBE_DATA_SOURCE
        assert item["data"]["data_source"] == PROBE_DATA_SOURCE

    async def test_skips_the_probes_that_already_exist_today(self):
        queue: asyncio.Queue = asyncio.Queue()
        today = [probe_finding_id(p["name"], utcnow().date()) for p in PROBES]

        assert await inject_probes(queue, _Data(today[1:])) == 1
        assert queue.get_nowait()["data"]["finding_id"] == today[0]
        assert await inject_probes(queue, _Data(today)) == 0
        assert queue.empty()


class TestTheScheduledTask:
    def test_registered_hourly_by_default(self):
        scheduler = TaskScheduler(SchedulerConfig())
        [task] = [t for t in scheduler._tasks if t.name == "probe_sweep"]
        assert task.interval == 3600

    def test_not_registered_when_disabled(self):
        scheduler = TaskScheduler(SchedulerConfig(probes_enabled=False))
        assert "probe_sweep" not in [t.name for t in scheduler._tasks]

    async def test_sweep_injects_onto_the_queue_it_was_handed(self):
        scheduler = TaskScheduler(SchedulerConfig())
        scheduler._data_service = _Data()
        queue: asyncio.Queue = asyncio.Queue()
        scheduler.set_processor_queue(queue)

        await scheduler._run_probe_sweep()

        assert queue.qsize() == 3
        assert scheduler.stats["probes_injected"] == 3


class TestTheProcessorGuard:
    def _processor(self, **config) -> FindingProcessor:
        processor = FindingProcessor(ProcessingConfig(**config))
        processor._evaluate_for_response = AsyncMock()
        return processor

    async def test_a_probe_never_reaches_response_evaluation(self):
        processor = self._processor(
            auto_triage_enabled=False, auto_enrich_enabled=False
        )
        probe = build_probe_finding(PROBES[0], utcnow().date())
        probe["severity"] = "critical"

        await processor._enrich_in_background(probe, PROBE_DATA_SOURCE)

        processor._evaluate_for_response.assert_not_awaited()
        assert processor.stats["queued_for_response"] == 0
        assert processor.stats["queued_for_investigation"] == 0

    async def test_an_ordinary_finding_still_does(self):
        processor = self._processor(
            auto_triage_enabled=False, auto_enrich_enabled=False
        )

        await processor._enrich_in_background({"finding_id": "f1", "severity": "high"})

        processor._evaluate_for_response.assert_awaited_once()

    # The guard sits after triage, not inside it: a probe that triage failed on
    # still stops, even when a severity that would normally respond is present.
    async def test_guard_holds_when_triage_raises(self):
        processor = self._processor(auto_triage_enabled=True, auto_enrich_enabled=False)
        probe = build_probe_finding(PROBES[0], utcnow().date())
        probe["severity"] = "critical"
        with patch.object(
            FindingProcessor,
            "_triage_finding",
            AsyncMock(side_effect=RuntimeError("down")),
        ):
            await processor._enrich_in_background(probe, PROBE_DATA_SOURCE)

        processor._evaluate_for_response.assert_not_awaited()


def test_a_probe_is_not_evidence_in_the_case_vocabulary():
    assert (
        resolve_source_tier("probe", InvestigationKind.CASE) is SourceTier.NOT_EVIDENCE
    )
