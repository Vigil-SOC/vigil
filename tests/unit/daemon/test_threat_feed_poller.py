# ThreatFeedPoller fetches and upserts, then offers the poll's uncovered keys to
# intake as one schedule row (#1009). It must not open the hunt itself.

from __future__ import annotations

from datetime import datetime

import pytest

from core.memory.hunt_coverage import build_proposal
from core.threat_intel import threat_feed_service as feed
from core.workflows.workflows_service import WorkflowsService
from services.daemon import orchestrator
from services.daemon import threat_feed_poller as poller
from services.daemon.threat_feed_poller import ThreatFeedPoller, _IntelIntake

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


def _offers(monkeypatch, *keys, already=(), queued=False):
    monkeypatch.setattr(
        "services.daemon.threat_feed_poller._intel_intake_state",
        lambda: _IntelIntake(queued, set(already)),
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
    assert summary["totals"] == {
        "seen": 2,
        "inserted": 2,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
    }
    assert summary["intake"] == {"inserted": 1, "keys": 1}


@pytest.mark.asyncio
async def test_a_poll_that_wrote_nothing_still_offers(monkeypatch):
    # The producer reads threat_indicators, not this poll's counters: keys an
    # earlier poll wrote and a refused insert left behind get another chance,
    # which a quiet TAXII `since` window would otherwise deny them forever.
    calls = []
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: calls.append("offered") or {"inserted": 1, "keys": 1},
    )
    _poll(monkeypatch, calls, counts={"inserted": 0, "updated": 0, "skipped": 2})

    summary = await ThreatFeedPoller().run_once()

    assert "offered" in calls
    assert summary["intake"] == {"inserted": 1, "keys": 1}


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


def test_a_queued_intel_row_holds_the_next_poll(monkeypatch):
    captured = _capture_intake(monkeypatch)
    _offers(monkeypatch, KEY, queued=True)
    monkeypatch.setattr(
        feed,
        "propose_hunts_from_recent_indicators",
        lambda: pytest.fail("a held poll must not spend a coverage check per key"),
    )

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert captured == []
    assert result == {"inserted": 0, "skipped": "intel_row_queued"}


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


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_the_watermark_and_the_next_poll_reasks(
    monkeypatch,
):
    # A raised fetch is the only signal run_once treats as a failed collection.
    # Returning [] used to look like a clean empty poll and slide added_after
    # past the outage, so indicators published then were never pulled.
    watermark = datetime(2026, 9, 29, 12, 0, 0)
    last_polled = {"cloudforce_one::col-1": watermark}
    monkeypatch.setattr(poller, "_last_polled", last_polled)
    failed = {"col-1": True}
    calls = []

    def _fetch(**kwargs):
        calls.append(kwargs)
        if failed.get(kwargs["collection_id"]):
            raise ConnectionError("503 Service Unavailable")
        return ["indicator"]

    def _upsert(indicators):
        return {"inserted": len(indicators), "updated": 0, "skipped": 0}

    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr("core.config.get_integration_config", lambda _id: CONFIG)
    monkeypatch.setattr(feed, "fetch_taxii_collection", _fetch)
    monkeypatch.setattr(feed, "upsert_indicators", _upsert)
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: {"inserted": 0},
    )

    poller_run = ThreatFeedPoller()
    summary = await poller_run.run_once()

    assert last_polled["cloudforce_one::col-1"] == watermark
    assert last_polled["cloudforce_one::col-2"] > watermark
    assert poller_run.stats["errors"] == 1
    assert summary["totals"]["errors"] == 1
    assert summary["collections"]["col-1"] == {"error": "503 Service Unavailable"}
    assert [call["collection_id"] for call in calls] == ["col-1", "col-2"]

    failed.clear()
    await poller_run.run_once()

    assert calls[2]["collection_id"] == "col-1"
    assert calls[2]["since"] == watermark


def test_a_refused_insert_is_reported_not_raised(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr("services.daemon.orchestrator.insert_intake_trigger", _boom)
    _offers(monkeypatch, KEY)

    assert ThreatFeedPoller().offer_uncovered_indicators_to_intake() == {
        "error": "no database"
    }


def _quiet_poll(monkeypatch, *, counts, config=CONFIG):
    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr("core.config.get_integration_config", lambda _id: config)
    monkeypatch.setattr(feed, "fetch_taxii_collection", lambda **_kw: ["indicator"])
    monkeypatch.setattr(feed, "upsert_indicators", lambda _ind: counts)
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: {"inserted": 0},
    )


@pytest.mark.asyncio
async def test_upsert_failures_hold_the_watermark_and_are_totalled(monkeypatch):
    watermark = datetime(2026, 9, 29, 12, 0, 0)
    last_polled = {"cloudforce_one::col-1": watermark}
    monkeypatch.setattr(poller, "_last_polled", last_polled)
    _quiet_poll(monkeypatch, counts={"inserted": 1, "updated": 0, "skipped": 2})

    run = ThreatFeedPoller()
    summary = await run.run_once()

    assert last_polled["cloudforce_one::col-1"] == watermark
    assert "cloudforce_one::col-2" not in last_polled
    assert summary["totals"]["skipped"] == 4
    assert run.stats["skipped"] == 4


@pytest.mark.asyncio
async def test_a_clean_upsert_advances_the_watermark(monkeypatch):
    last_polled = {}
    monkeypatch.setattr(poller, "_last_polled", last_polled)
    _quiet_poll(monkeypatch, counts={"inserted": 1, "updated": 0, "skipped": 0})

    await ThreatFeedPoller().run_once()

    assert set(last_polled) == {"cloudforce_one::col-1", "cloudforce_one::col-2"}


@pytest.mark.asyncio
async def test_a_missing_taxii_client_is_a_poll_error_not_a_clean_empty_fetch(
    monkeypatch,
):
    last_polled = {}
    monkeypatch.setattr(poller, "_last_polled", last_polled)
    _quiet_poll(monkeypatch, counts={"inserted": 0, "updated": 0, "skipped": 0})

    def _no_wheel(**_kwargs):
        raise ModuleNotFoundError("No module named 'taxii2client'")

    monkeypatch.setattr(feed, "fetch_taxii_collection", _no_wheel)

    run = ThreatFeedPoller()
    summary = await run.run_once()

    assert last_polled == {}
    assert run.stats["errors"] == 2
    assert summary["totals"]["errors"] == 2


@pytest.mark.asyncio
async def test_incomplete_config_warns_once_per_state_and_is_counted(
    monkeypatch, caplog
):
    monkeypatch.setattr(poller, "_last_skip_signature", None)
    config = {"api_token": "s3cret-token", "collection_ids": "col-1"}
    _quiet_poll(monkeypatch, counts={}, config=config)
    run = ThreatFeedPoller()

    with caplog.at_level("DEBUG", logger=poller.logger.name):
        assert await run.run_once() == {"skipped": "incomplete_config"}
        await run.run_once()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "taxii_server_url" in warnings[0].getMessage()

        config.pop("api_token")
        await run.run_once()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "api_token" in warnings[1].getMessage()
    assert run.stats["skipped_incomplete"] == 3
    assert "s3cret-token" not in caplog.text

    config.update(api_token="s3cret-token", taxii_server_url="https://taxii.example")
    await run.run_once()
    config.pop("taxii_server_url")
    await run.run_once()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 3
