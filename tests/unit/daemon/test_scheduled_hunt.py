# The scheduled "threat hunt" used to tally MITRE techniques into a dict and drop
# it on the floor. It now opens a real hunt on the orchestrator's intake.

from __future__ import annotations

from typing import List

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


def _scheduler(findings=None, data=None) -> TaskScheduler:
    scheduler = TaskScheduler(SchedulerConfig())
    scheduler._data_service = data if data is not None else _Data(findings)
    return scheduler


def _finding(*techniques):
    return {"mitre_predictions": {t: 0.9 for t in techniques}}


def _capture_intake(monkeypatch) -> List[dict]:
    rows: List[dict] = []

    def fake(**kwargs):
        rows.append(kwargs)
        return 1

    monkeypatch.setattr("services.daemon.orchestrator.insert_intake_trigger", fake)
    return rows


class TestTheScheduledHuntOpensARun:
    async def test_puts_a_threat_hunt_on_the_orchestrators_intake(self, monkeypatch):
        rows = _capture_intake(monkeypatch)
        scheduler = _scheduler()

        await scheduler._run_threat_hunt()

        assert rows[0]["kind"] == "schedule"
        assert rows[0]["payload"]["workflow_id"] == "threat-hunt"

    # An analyst's request and a nightly sweep share the intake but are not the
    # same event, so the row an operator reads must tell them apart.
    async def test_marks_it_as_scheduled_rather_than_asked_for(self, monkeypatch):
        rows = _capture_intake(monkeypatch)
        scheduler = _scheduler()

        await scheduler._run_threat_hunt()

        assert rows[0]["payload"]["trigger_type"] == "scheduled"

    async def test_counts_the_hunt_it_opened(self, monkeypatch):
        _capture_intake(monkeypatch)
        scheduler = _scheduler()

        await scheduler._run_threat_hunt()

        assert scheduler.stats["threat_hunts"] == 1


class TestWhatTheHuntIsSteeredToward:
    async def test_asks_about_the_techniques_the_estate_is_showing(self, monkeypatch):
        rows = _capture_intake(monkeypatch)
        scheduler = _scheduler(
            [_finding("T1071.001"), _finding("T1071.001"), _finding("T1078")]
        )

        await scheduler._run_threat_hunt()

        hypothesis = rows[0]["payload"]["hypothesis"]
        assert "T1071.001" in hypothesis
        assert "T1078" in hypothesis

    # The definition states hypotheses of its own, so a hunt with nothing to add
    # is still a hunt. It must not be steered toward an empty claim.
    async def test_falls_back_to_the_definitions_own_hypotheses(self, monkeypatch):
        rows = _capture_intake(monkeypatch)
        scheduler = _scheduler([])

        await scheduler._run_threat_hunt()

        assert rows[0]["payload"]["hypothesis"] == ""

    async def test_still_opens_the_hunt_when_the_findings_cannot_be_read(
        self, monkeypatch
    ):
        rows = _capture_intake(monkeypatch)
        scheduler = _scheduler(data=_Angry())

        await scheduler._run_threat_hunt()

        assert rows[0]["payload"]["workflow_id"] == "threat-hunt"
        assert rows[0]["payload"]["hypothesis"] == ""


# The legacy body computed these and dropped the result. Nothing consumed them,
# and _extract_iocs excluded all of 172.0-172.255 rather than the RFC1918 block.
def test_the_legacy_ioc_tallying_is_gone():
    for dead in ("_hunt_for_iocs", "_extract_iocs", "_analyze_finding_patterns"):
        assert not hasattr(TaskScheduler, dead), f"{dead} still exists"
