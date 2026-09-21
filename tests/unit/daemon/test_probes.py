# Three known-answer probes go through store → triage once a day and stop there
# (#923): never the responder, never the orchestrator.

from __future__ import annotations

import asyncio
from datetime import date, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.memory.source_tier import InvestigationKind, SourceTier, resolve_source_tier
from core.time import utcnow
from services.daemon import metrics as daemon_metrics
from services.daemon.config import ProcessingConfig, SchedulerConfig
from services.daemon.probes import (
    ACTIONS,
    PROBE_DATA_SOURCE,
    PROBES,
    SEVERITIES,
    build_probe_finding,
    inject_probes,
    probe_finding_id,
    score_probes,
)
from services.daemon.processor import FindingProcessor
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit


class _Data:
    def __init__(self, existing=(), rows=()):
        self.existing = set(existing)
        self.rows = {r["finding_id"]: r for r in rows}
        self.updates = []
        self.calls = []

    def get_finding(self, finding_id):
        self.calls.append("get_finding")
        if finding_id in self.rows:
            return self.rows[finding_id]
        return {"finding_id": finding_id} if finding_id in self.existing else None

    def get_findings(self, data_source=None, **_):
        self.calls.append("get_findings")
        return [r for r in self.rows.values() if r["data_source"] == data_source]

    def update_finding(self, finding_id, **updates):
        self.updates.append((finding_id, updates))
        self.rows[finding_id].update(updates)
        return True


def _probe_row(probe, age=timedelta(hours=2), triage=None, triage_after=None):
    """A stored probe as the data service dumps it: aware ISO ``created_at``,
    triage timestamp naive, as the processor writes it."""
    created = utcnow() - age
    row = build_probe_finding(probe, created.date())
    row["created_at"] = created.replace(tzinfo=timezone.utc).isoformat()
    # What a timed-out triage leaves behind (#965): an error, no ai_triage.
    row["ai_enrichment"] = {"ai_triage_error": "timed out"}
    if triage is not None:
        answered = created + (triage_after or timedelta(minutes=3))
        row["ai_enrichment"] = {
            "ai_triage": {"timestamp": answered.isoformat(), "result": triage}
        }
    return row


def _score(data):
    scored = score_probes(data)
    return scored, {
        fid: r["entity_context"]["probe"].get("score") for fid, r in data.rows.items()
    }


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


class TestScoring:
    beacon, patch_window, travel = PROBES

    def test_the_three_outcomes(self):
        data = _Data(
            rows=[
                _probe_row(
                    self.beacon,
                    triage={
                        "severity": "critical",
                        "recommended_action": "isolate",
                        "confidence": 0.9,
                    },
                ),
                _probe_row(
                    self.patch_window,
                    triage={"severity": "high", "recommended_action": "dismiss"},
                ),
                _probe_row(self.travel),
            ]
        )

        scored, scores = _score(data)

        assert scored == 3
        hit, miss, silent = (scores[r["finding_id"]] for r in data.rows.values())
        assert hit["outcome"] == "hit"
        assert hit["verdict"] == {
            "severity": "critical",
            "recommended_action": "isolate",
            "confidence": 0.9,
        }
        assert hit["time_to_verdict_s"] == pytest.approx(180)
        assert hit["scored_at"]
        assert miss["outcome"] == "miss" and miss["time_to_verdict_s"] > 0
        assert silent["outcome"] == "silent"
        assert silent["verdict"] is None and silent["time_to_verdict_s"] is None

    def test_a_right_severity_with_a_wrong_action_is_a_miss(self):
        data = _Data(
            rows=[
                _probe_row(
                    self.travel,
                    triage={"severity": "high", "recommended_action": "isolate"},
                )
            ]
        )
        _, scores = _score(data)
        assert [s["outcome"] for s in scores.values()] == ["miss"]

    def test_a_probe_younger_than_an_hour_waits(self):
        data = _Data(rows=[_probe_row(self.travel, age=timedelta(minutes=59))])

        scored, scores = _score(data)

        assert scored == 0 and data.updates == []
        assert list(scores.values()) == [None]

    def test_a_scored_probe_is_not_rescored(self):
        data = _Data(rows=[_probe_row(self.travel)])
        assert score_probes(data) == 1
        [(_, first)] = data.updates

        # A triage arriving later (enrichment backfill) does not reopen it.
        data.rows[next(iter(data.rows))]["ai_enrichment"] = {
            "ai_triage": {
                "timestamp": utcnow().isoformat(),
                "result": {"severity": "high", "recommended_action": "investigate"},
            }
        }
        assert score_probes(data) == 0
        assert data.updates == [(next(iter(data.rows)), first)]

    def test_the_score_is_merged_into_entity_context(self):
        row = _probe_row(
            self.beacon,
            triage={"severity": "high", "recommended_action": "block"},
        )
        data = _Data(rows=[row])

        score_probes(data)

        [(finding_id, updates)] = data.updates
        assert finding_id == row["finding_id"]
        assert set(updates) == {"entity_context"}
        ctx = updates["entity_context"]
        assert ctx["src_ips"] == self.beacon["entity_context"]["src_ips"]
        assert ctx["probe"]["name"] == self.beacon["name"]
        assert ctx["probe"]["expected"] == self.beacon["expected"]
        assert ctx["probe"]["score"]["outcome"] == "hit"

    # OTEL off is the default in tests: get_meter hands back the no-op meter.
    def test_scores_without_otel(self):
        fresh = daemon_metrics.ProbeMetrics()
        data = _Data(rows=[_probe_row(self.travel)])
        with patch("services.daemon.probes.probe_metrics", fresh):
            assert score_probes(data) == 1
        assert fresh.results[(self.travel["name"], "silent")] == 1
        assert fresh._results_counter is not None  # the no-op instrument

    def test_scores_when_the_meter_itself_fails(self):
        fresh = daemon_metrics.ProbeMetrics()
        data = _Data(rows=[_probe_row(self.travel)])
        with (
            patch.object(
                daemon_metrics, "get_meter", side_effect=RuntimeError("no otel")
            ),
            patch("services.daemon.probes.probe_metrics", fresh),
        ):
            assert score_probes(data) == 1
        assert fresh.results[(self.travel["name"], "silent")] == 1

    def test_a_malformed_row_does_not_stop_the_others(self):
        bad = _probe_row(self.beacon, triage="not-a-dict")
        good = _probe_row(self.travel)
        data = _Data(rows=[bad, good])

        assert score_probes(data) == 1
        assert good["entity_context"]["probe"]["score"]["outcome"] == "silent"
        assert "score" not in bad["entity_context"]["probe"]

    def test_the_instruments_get_the_labels_and_only_verdicts_reach_the_histogram(
        self,
    ):
        fresh = daemon_metrics.ProbeMetrics()
        data = _Data(
            rows=[
                _probe_row(
                    self.beacon,
                    triage={"severity": "high", "recommended_action": "block"},
                ),
                _probe_row(self.travel),
            ]
        )
        with (
            patch.object(daemon_metrics, "get_meter") as meter,
            patch("services.daemon.probes.probe_metrics", fresh),
        ):
            score_probes(data)
        counter = meter.return_value.create_counter.return_value
        hist = meter.return_value.create_histogram.return_value
        assert meter.return_value.create_counter.call_args.kwargs["name"] == (
            "vigil.probe.results.total"
        )
        assert meter.return_value.create_histogram.call_args.kwargs["name"] == (
            "vigil.probe.time_to_verdict.seconds"
        )
        assert [c.args[1] for c in counter.add.call_args_list] == [
            {"probe": self.beacon["name"], "outcome": "hit"},
            {"probe": self.travel["name"], "outcome": "silent"},
        ]
        assert [c.args[1] for c in hist.record.call_args_list] == [
            {"probe": self.beacon["name"]}
        ]


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

    async def test_sweep_scores_before_it_injects(self):
        scheduler = TaskScheduler(SchedulerConfig())
        scheduler._data_service = _Data(
            rows=[_probe_row(PROBES[0], age=timedelta(days=1))]
        )
        queue: asyncio.Queue = asyncio.Queue()
        scheduler.set_processor_queue(queue)

        await scheduler._run_probe_sweep()

        assert scheduler.stats["probes_scored"] == 1
        # Yesterday's row was scored and today's three still went on the queue.
        assert queue.qsize() == 3
        # The scoring read came before the first injection lookup.
        assert scheduler._data_service.calls[0] == "get_findings"
        assert scheduler._data_service.calls.count("get_finding") == 3


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
