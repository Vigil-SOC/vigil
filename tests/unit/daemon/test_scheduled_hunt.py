# The scheduled "threat hunt" used to tally MITRE techniques into a dict and drop
# it on the floor. It now opens a real hunt on the orchestrator's intake.

from __future__ import annotations

import asyncio
from typing import Optional, Tuple

import pytest

from services.daemon.config import SchedulerConfig
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit


class _Data:
    def __init__(self, findings=None):
        self._findings = findings or []

    def get_findings(self, limit=None):
        return self._findings


class _Angry:
    def get_findings(self, limit=None):
        raise RuntimeError("the database is down")


def _scheduler(
    findings=None, queue=True, data=None
) -> Tuple[TaskScheduler, Optional[asyncio.Queue]]:
    scheduler = TaskScheduler(SchedulerConfig())
    scheduler._data_service = data if data is not None else _Data(findings)
    if not queue:
        return scheduler, None
    intake: asyncio.Queue = asyncio.Queue()
    scheduler.set_investigation_queue(intake)
    return scheduler, intake


def _finding(*techniques):
    return {"mitre_predictions": {t: 0.9 for t in techniques}}


class TestTheScheduledHuntOpensARun:
    async def test_puts_a_threat_hunt_on_the_orchestrators_intake(self):
        scheduler, intake = _scheduler()

        await scheduler._run_threat_hunt()

        item = intake.get_nowait()
        assert item["workflow_id"] == "threat-hunt"
        assert item["type"] == "manual"

    # An analyst's request and a nightly sweep share the intake but are not the
    # same event, so the row an operator reads must tell them apart.
    async def test_marks_it_as_scheduled_rather_than_asked_for(self):
        scheduler, intake = _scheduler()

        await scheduler._run_threat_hunt()

        assert intake.get_nowait()["trigger_type"] == "scheduled"

    async def test_counts_the_hunt_it_opened(self):
        scheduler, _ = _scheduler()

        await scheduler._run_threat_hunt()

        assert scheduler.stats["threat_hunts"] == 1

    # Queued rather than run here: the orchestrator owns the investigation record,
    # the budget and the reconcile, and a second path to those is a second set of
    # guardrails to keep in step.
    async def test_opens_nothing_when_the_daemon_wired_no_intake(self):
        scheduler, _ = _scheduler(queue=False)

        await scheduler._run_threat_hunt()

        assert scheduler._investigation_queue is None


class TestWhatTheHuntIsSteeredToward:
    async def test_asks_about_the_techniques_the_estate_is_showing(self):
        scheduler, intake = _scheduler(
            [_finding("T1071.001"), _finding("T1071.001"), _finding("T1078")]
        )

        await scheduler._run_threat_hunt()

        hypothesis = intake.get_nowait()["hypothesis"]
        assert "T1071.001" in hypothesis
        assert "T1078" in hypothesis

    # The definition states hypotheses of its own, so a hunt with nothing to add
    # is still a hunt. It must not be steered toward an empty claim.
    async def test_falls_back_to_the_definitions_own_hypotheses(self):
        scheduler, intake = _scheduler([])

        await scheduler._run_threat_hunt()

        assert intake.get_nowait()["hypothesis"] == ""

    async def test_still_opens_the_hunt_when_the_findings_cannot_be_read(self):
        scheduler, intake = _scheduler(data=_Angry())

        await scheduler._run_threat_hunt()

        item = intake.get_nowait()
        assert item["workflow_id"] == "threat-hunt"
        assert item["hypothesis"] == ""


# The legacy body computed these and dropped the result. Nothing consumed them,
# and _extract_iocs excluded all of 172.0-172.255 rather than the RFC1918 block.
def test_the_legacy_ioc_tallying_is_gone():
    for dead in ("_hunt_for_iocs", "_extract_iocs", "_analyze_finding_patterns"):
        assert not hasattr(TaskScheduler, dead), f"{dead} still exists"
