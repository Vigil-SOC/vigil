# ThreatFeedPoller.run_once only fetches and upserts (#905). Proposing hunts
# from what it wrote is the propose_feed_hunts tool's job, never the poller's.

from __future__ import annotations

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


@pytest.mark.asyncio
async def test_run_once_only_fetches_and_upserts(monkeypatch):
    calls = []

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the poller must not start a hunt or queue a case")

    monkeypatch.setattr(WorkflowsService, "execute_workflow", _forbidden)
    monkeypatch.setattr(orchestrator, "insert_intake_trigger", _forbidden)
    monkeypatch.setattr(orchestrator.Orchestrator, "_enqueue_investigation", _forbidden)
    monkeypatch.setattr(feed, "propose_hunts_from_recent_indicators", _forbidden)
    monkeypatch.setattr("core.memory.hunt_coverage.check_coverage", _forbidden)

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
    ]
    assert summary["totals"] == {"seen": 2, "inserted": 2, "updated": 0, "errors": 0}
