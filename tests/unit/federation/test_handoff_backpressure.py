"""A full hand-off makes Federation wait, and shutdown still wins."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from core.federation.contract import FetchResult
from core.federation.runner import FederationRunner
from core.ingestion.handoff import put_or_shutdown

pytestmark = pytest.mark.unit


class _Adapter:
    name = "flooded"

    def __init__(self, findings: List[Dict[str, Any]]):
        self._findings = findings

    async def fetch(self, *, since, cursor, max_items):
        return FetchResult(findings=list(self._findings), cursor={"page": "next"})


class _Dedup:
    def __init__(self):
        self.processed: set = set()

    async def is_processed(self, key: str) -> bool:
        return key in self.processed

    async def mark_processed(self, key: str) -> None:
        self.processed.add(key)


def _finding(n: int) -> Dict[str, Any]:
    return {"finding_id": f"f-{n}", "external_id": f"e-{n}", "severity": "high"}


def _runner(monkeypatch, queue, findings):
    runner = FederationRunner(output_queue=queue)
    adapter = _Adapter(findings)
    dedup = _Dedup()
    runner._dedup[adapter.name] = dedup  # type: ignore[assignment]
    saved: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, *, cursor: saved.append(cursor),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda *a, **k: pytest.fail("a shutdown is not a failed fetch"),
    )
    return runner, adapter, dedup, saved


@pytest.mark.asyncio
async def test_shutdown_during_a_full_hand_off_keeps_the_cursor(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("already waiting")
    shutdown = asyncio.Event()
    runner, adapter, dedup, saved = _runner(monkeypatch, queue, [_finding(1)])
    runner._shutdown = shutdown

    tick = asyncio.create_task(runner._do_one_tick(adapter, {"cursor": {}}))
    await asyncio.sleep(0.05)
    assert not tick.done()  # waiting for room, not dropping

    shutdown.set()
    more = await asyncio.wait_for(tick, timeout=1)

    assert more is False
    assert saved == []
    assert dedup.processed == set()
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_a_full_hand_off_waits_then_takes_the_whole_page(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    runner, adapter, dedup, saved = _runner(
        monkeypatch, queue, [_finding(1), _finding(2), _finding(3)]
    )

    tick = asyncio.create_task(runner._do_one_tick(adapter, {"cursor": {}}))
    taken = []
    for _ in range(3):
        taken.append(await asyncio.wait_for(queue.get(), timeout=1))
    await asyncio.wait_for(tick, timeout=1)

    assert [t["data"]["external_id"] for t in taken] == ["e-1", "e-2", "e-3"]
    assert saved == [{"page": "next"}]
    assert dedup.processed == {"e-1", "e-2", "e-3"}


@pytest.mark.asyncio
async def test_put_or_shutdown_puts_nothing_once_shutdown_is_set():
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    shutdown = asyncio.Event()
    shutdown.set()

    assert await put_or_shutdown(queue, "x", shutdown) is False
    assert queue.empty()


@pytest.fixture
def full_waits(monkeypatch):
    from collections import defaultdict

    from core.ingestion import handoff

    counts: dict = defaultdict(int)
    monkeypatch.setattr(handoff, "full_waits", counts)
    return counts


@pytest.mark.asyncio
async def test_a_queue_with_room_takes_the_item_without_counting_a_wait(full_waits):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    assert await put_or_shutdown(queue, {"source": "splunk"}, asyncio.Event())

    assert queue.qsize() == 1
    assert dict(full_waits) == {}


@pytest.mark.asyncio
async def test_a_full_queue_counts_the_wait_and_shutdown_still_wins(full_waits):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("already waiting")
    shutdown = asyncio.Event()

    put = asyncio.create_task(put_or_shutdown(queue, {"source": "splunk"}, shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()

    assert await asyncio.wait_for(put, timeout=1) is False
    assert dict(full_waits) == {"splunk": 1}
    assert queue.qsize() == 1
