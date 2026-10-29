"""Findings the API stores are triaged on a Redis signal, not at the next sweep."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from core.ingestion import triage_signal
from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit


def _processor(**cfg):
    p = FindingProcessor(ProcessingConfig(**cfg))
    p._data_service = MagicMock()
    return p


def _record_enrich(processor, gate=None):
    seen = []

    async def fake(finding, source=None):
        seen.append(finding["finding_id"])
        if gate is not None:
            await gate.wait()

    processor._enrich_in_background = fake
    return seen


class _Pubsub:
    """Yields the queued messages, then None (a quiet poll) until shutdown."""

    def __init__(self, messages, shutdown):
        self.messages, self.shutdown = list(messages), shutdown
        self.quiet_polls = 0

    async def get_message(self, **_):
        if self.messages:
            return {"data": json.dumps(self.messages.pop(0))}
        self.quiet_polls += 1
        if self.quiet_polls > 1:
            self.shutdown.set()
        return None


@pytest.mark.asyncio
async def test_signalled_ids_are_triaged_once_for_a_burst():
    processor = _processor()
    processor._data_service.get_findings_missing_enrichment.return_value = [
        {"finding_id": "f-1"},
        {"finding_id": "f-2"},
    ]
    seen = _record_enrich(processor)
    shutdown = asyncio.Event()

    pubsub = _Pubsub([["f-1"], ["f-2", "f-1"]], shutdown)
    await processor._drain_signals(pubsub, shutdown)
    await asyncio.gather(*processor._enrich_tasks)

    query = processor._data_service.get_findings_missing_enrichment
    assert query.call_count == 1  # two messages, one batch
    assert sorted(query.call_args.kwargs["finding_ids"]) == ["f-1", "f-2"]
    assert sorted(seen) == ["f-1", "f-2"]


@pytest.mark.asyncio
async def test_open_breaker_ignores_the_signal():
    processor = _processor()
    processor._enrich_paused_until = float("inf")
    seen = _record_enrich(processor)

    await processor._triage_signalled(["f-1"], asyncio.Event())

    processor._data_service.get_findings_missing_enrichment.assert_not_called()
    assert seen == []


@pytest.mark.asyncio
async def test_in_flight_finding_is_not_triaged_twice():
    processor = _processor()
    gate = asyncio.Event()
    seen = _record_enrich(processor, gate)

    await processor._spawn_enrich({"finding_id": "f-1"})
    await processor._spawn_enrich({"finding_id": "f-1"})  # sweep / signal repeat
    await asyncio.sleep(0)
    assert seen == ["f-1"]

    gate.set()
    await asyncio.gather(*processor._enrich_tasks)
    await asyncio.sleep(0)
    # Done, so a later pass may triage it again (e.g. after a recorded failure).
    await processor._spawn_enrich({"finding_id": "f-1"})
    await asyncio.gather(*processor._enrich_tasks)
    assert seen == ["f-1", "f-1"]


@pytest.mark.asyncio
async def test_without_redis_the_sweep_still_triages(monkeypatch):
    def refuse(*_a, **_k):
        raise ConnectionError("no redis")

    monkeypatch.setattr("services.daemon.processor.open_async_redis", refuse)
    monkeypatch.setattr(triage_signal, "open_sync_redis", refuse)
    processor = _processor(enrich_backfill_interval=0.01)
    processor._data_service.get_findings_missing_enrichment.return_value = [
        {"finding_id": "f-9"}
    ]
    seen = _record_enrich(processor)
    shutdown = asyncio.Event()

    triage_signal.signal_new_findings(["f-9"])  # API side: must not raise
    listener = asyncio.create_task(processor._signal_loop(shutdown))
    sweep = asyncio.create_task(processor._backfill_loop(shutdown))
    await asyncio.sleep(0.1)
    shutdown.set()
    await asyncio.gather(listener, sweep)

    assert "f-9" in seen


def test_signal_publishes_a_capped_deduped_id_list(monkeypatch):
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(triage_signal, "open_sync_redis", lambda **_: client)

    triage_signal.signal_new_findings(["a", "a", "", "b"])
    channel, payload = client.publish.call_args.args
    assert channel == triage_signal.CHANNEL
    assert json.loads(payload) == ["a", "b"]

    client.publish.reset_mock()
    triage_signal.signal_new_findings([])
    client.publish.assert_not_called()
