# ThreatFeedPoller fetches and upserts, then offers the poll's uncovered keys to
# intake as one schedule row (#1009). It must not open the hunt itself.

from __future__ import annotations

import pytest

from core.memory.hunt_coverage import build_proposal
from core.threat_intel import threat_feed_service as feed
from core.workflows.workflows_service import WorkflowsService
from services.daemon import orchestrator
from services.daemon.threat_feed_poller import ThreatFeedPoller

pytestmark = pytest.mark.unit

CONFIG = {
    "api_token": "tok",
    "taxii_server_url": "https://taxii.example",
    "collection_ids": "col-1, col-2",
}

KEY = "ip:203.0.113.7"
OTHER_KEY = "domain:evil.example"


def _proposal(key):
    body = build_proposal([key], [])
    return {"entity_key": key, "proposal": body}


def _forbid_hunt_start(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the poller must not start a hunt or queue a case")

    monkeypatch.setattr(WorkflowsService, "execute_workflow", _forbidden)
    monkeypatch.setattr(orchestrator.Orchestrator, "_enqueue_investigation", _forbidden)


def _capture_intake(monkeypatch):
    captured = []

    def insert(**kwargs):
        captured.append(kwargs)
        return 1

    monkeypatch.setattr("services.daemon.orchestrator.insert_intake_trigger", insert)
    return captured


def _offers(monkeypatch, *keys, already=()):
    monkeypatch.setattr(
        "services.daemon.threat_feed_poller._keys_already_offered",
        lambda: set(already),
    )
    monkeypatch.setattr(
        feed,
        "propose_hunts_from_recent_indicators",
        lambda: {"proposals": [_proposal(key) for key in keys]},
    )


def _poll(monkeypatch, calls, *, counts):
    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr("core.config.get_integration_config", lambda _id: CONFIG)

    def _fetch(**kwargs):
        calls.append(("fetch", kwargs["collection_id"]))
        return ["indicator"]

    def _upsert(indicators):
        calls.append(("upsert", len(indicators)))
        return counts

    monkeypatch.setattr(feed, "fetch_taxii_collection", _fetch)
    monkeypatch.setattr(feed, "upsert_indicators", _upsert)


@pytest.mark.asyncio
async def test_run_once_fetches_upserts_then_offers(monkeypatch):
    calls = []
    _forbid_hunt_start(monkeypatch)
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: calls.append("offered") or {"inserted": 1, "keys": 1},
    )
    _poll(monkeypatch, calls, counts={"inserted": 1, "updated": 0, "skipped": 0})

    summary = await ThreatFeedPoller().run_once()

    assert calls == [
        ("fetch", "col-1"),
        ("upsert", 1),
        ("fetch", "col-2"),
        ("upsert", 1),
        "offered",
    ]
    assert summary["totals"] == {"seen": 2, "inserted": 2, "updated": 0, "errors": 0}
    assert summary["intake"] == {"inserted": 1, "keys": 1}


@pytest.mark.asyncio
async def test_a_poll_that_wrote_nothing_does_not_offer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: calls.append("offered") or {},
    )
    _poll(monkeypatch, calls, counts={"inserted": 0, "updated": 0, "skipped": 2})

    summary = await ThreatFeedPoller().run_once()

    assert "offered" not in calls
    assert "intake" not in summary


@pytest.mark.asyncio
async def test_a_disabled_poll_does_not_offer(monkeypatch):
    offered = []
    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: offered.append("offered") or {},
    )

    summary = await ThreatFeedPoller().run_once()

    assert summary == {"skipped": "integration_disabled"}
    assert offered == []


def test_a_poll_of_uncovered_keys_is_one_row_naming_all_of_them(monkeypatch):
    _forbid_hunt_start(monkeypatch)
    captured = _capture_intake(monkeypatch)
    _offers(monkeypatch, KEY, OTHER_KEY)

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert result == {"inserted": 1, "keys": 2, "skipped_recent": 0}
    assert len(captured) == 1
    payload = captured[0]["payload"]
    assert captured[0]["kind"] == "schedule"
    assert captured[0]["priority"] == "low"
    # The hypothesis and the subjects key are the same string, or kept_subjects
    # drops the subjects on the way to the board.
    statement = payload["hypothesis"]
    assert payload["hypothesis_subjects"] == {statement: [KEY, OTHER_KEY]}
    assert payload == {
        "workflow_id": "threat-hunt",
        "trigger_type": "intel",
        "finding_ids": [],
        "hypothesis": statement,
        "hypothesis_subjects": {statement: [KEY, OTHER_KEY]},
    }


def test_no_uncovered_key_is_no_row(monkeypatch):
    captured = _capture_intake(monkeypatch)
    _offers(monkeypatch)

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert captured == []
    assert result == {"inserted": 0, "keys": 0, "skipped_recent": 0}


def test_a_key_already_offered_is_left_out_and_the_rest_still_go(monkeypatch):
    captured = _capture_intake(monkeypatch)
    _offers(monkeypatch, KEY, OTHER_KEY, already=[KEY])

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert result == {"inserted": 1, "keys": 1, "skipped_recent": 1}
    subjects = captured[0]["payload"]["hypothesis_subjects"]
    assert list(subjects.values()) == [[OTHER_KEY]]


def test_every_key_already_offered_is_no_row(monkeypatch):
    captured = _capture_intake(monkeypatch)
    _offers(monkeypatch, KEY, OTHER_KEY, already=[KEY, OTHER_KEY])

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert captured == []
    assert result == {"inserted": 0, "keys": 0, "skipped_recent": 2}


def test_a_refused_insert_is_reported_not_raised(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr("services.daemon.orchestrator.insert_intake_trigger", _boom)
    _offers(monkeypatch, KEY)

    assert ThreatFeedPoller().offer_uncovered_indicators_to_intake() == {
        "error": "no database"
    }
