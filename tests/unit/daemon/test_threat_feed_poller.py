# ThreatFeedPoller fetches and upserts, then offers uncovered indicators to
# intake as schedule rows (#1009). It must not open the hunt itself.

from __future__ import annotations

from types import SimpleNamespace

import pytest

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

HYPOTHESIS = (
    "Activity from the reported indicators ip:203.0.113.7 is present in the "
    "environment"
)
PROPOSAL = {
    "entity_key": "ip:203.0.113.7",
    "proposal": {
        "hypothesis": HYPOTHESIS,
        "hypothesis_subjects": {HYPOTHESIS: ["ip:203.0.113.7"]},
    },
}


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


@pytest.mark.asyncio
async def test_run_once_fetches_upserts_then_offers(monkeypatch):
    calls = []
    _forbid_hunt_start(monkeypatch)
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: calls.append("offered") or {"inserted": 0, "skipped_queued": 0},
    )
    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: True))
    monkeypatch.setattr("core.config.get_integration_config", lambda _id: CONFIG)

    def _fetch(**kwargs):
        calls.append(("fetch", kwargs["collection_id"]))
        return ["indicator"]

    def _upsert(indicators):
        calls.append(("upsert", len(indicators)))
        return {"inserted": 1, "updated": 0, "skipped": 0}

    monkeypatch.setattr(feed, "fetch_taxii_collection", _fetch)
    monkeypatch.setattr(feed, "upsert_indicators", _upsert)

    summary = await ThreatFeedPoller().run_once()

    assert calls == [
        ("fetch", "col-1"),
        ("upsert", 1),
        ("fetch", "col-2"),
        ("upsert", 1),
        "offered",
    ]
    assert summary["totals"] == {"seen": 2, "inserted": 2, "updated": 0, "errors": 0}
    assert summary["intake"] == {"inserted": 0, "skipped_queued": 0}


@pytest.mark.asyncio
async def test_a_disabled_poll_does_not_offer(monkeypatch):
    offered = []
    monkeypatch.setattr(ThreatFeedPoller, "is_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(
        ThreatFeedPoller,
        "offer_uncovered_indicators_to_intake",
        lambda self: offered.append("offered") or {"inserted": 0},
    )

    summary = await ThreatFeedPoller().run_once()

    assert summary == {"skipped": "integration_disabled"}
    assert offered == []


def test_uncovered_indicator_becomes_a_schedule_row(monkeypatch):
    _forbid_hunt_start(monkeypatch)
    captured = _capture_intake(monkeypatch)
    monkeypatch.setattr(
        "services.daemon.threat_feed_poller._queued_intel_entity_keys",
        lambda: set(),
    )
    monkeypatch.setattr(
        feed,
        "propose_hunts_from_recent_indicators",
        lambda: {"proposals": [PROPOSAL]},
    )

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert result == {"inserted": 1, "skipped_queued": 0}
    assert captured == [
        {
            "kind": "schedule",
            "priority": "low",
            "payload": {
                "workflow_id": "threat-hunt",
                "trigger_type": "intel",
                "finding_ids": [],
                "hypothesis": HYPOTHESIS,
                "hypothesis_subjects": {HYPOTHESIS: ["ip:203.0.113.7"]},
                "entity_key": "ip:203.0.113.7",
            },
        }
    ]


def test_covered_indicator_is_not_enqueued(monkeypatch):
    captured = _capture_intake(monkeypatch)
    monkeypatch.setattr(
        "services.daemon.threat_feed_poller._queued_intel_entity_keys",
        lambda: set(),
    )
    monkeypatch.setattr(
        feed,
        "propose_hunts_from_recent_indicators",
        lambda: {
            "proposals": [],
            "running": 1,
            "concluded": 1,
        },
    )

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert captured == []
    assert result == {"inserted": 0, "skipped_queued": 0}


def test_second_poll_while_queued_does_not_duplicate(monkeypatch):
    captured = _capture_intake(monkeypatch)
    monkeypatch.setattr(
        "services.daemon.threat_feed_poller._queued_intel_entity_keys",
        lambda: {"ip:203.0.113.7"},
    )
    monkeypatch.setattr(
        feed,
        "propose_hunts_from_recent_indicators",
        lambda: {"proposals": [PROPOSAL]},
    )

    result = ThreatFeedPoller().offer_uncovered_indicators_to_intake()

    assert captured == []
    assert result == {"inserted": 0, "skipped_queued": 1}


def test_a_queued_nightly_hunt_does_not_block_an_intel_offer(monkeypatch):
    from services.daemon.threat_feed_poller import _queued_intel_entity_keys

    rows = [
        SimpleNamespace(
            payload={
                "trigger_type": "scheduled",
                "workflow_id": "threat-hunt",
                "hypothesis": "nightly",
            }
        ),
        SimpleNamespace(
            payload={"trigger_type": "intel", "entity_key": "ip:203.0.113.7"}
        ),
    ]

    class Session:
        def query(self, model):
            return self

        def filter(self, *a, **k):
            return self

        def all(self):
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    db = SimpleNamespace(session_scope=lambda: Session())
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)

    assert _queued_intel_entity_keys() == {"ip:203.0.113.7"}
