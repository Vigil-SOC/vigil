"""A full federation batch must not advance the cursor past unread alerts (#1233).

``SIEMIngestionAdapter.fetch`` used to return ``fresh_cursor()`` (now) whether
or not the batch filled ``max_items``. When more than ``max_items`` alerts
arrived between two ticks, the next tick started at now and the rest of that
window was never fetched. The cursor now stops at the newest alert returned
when the batch was full, and only moves to now when the batch was short.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from core.federation.adapters._base import (
    cursor_at,
    parse_alert_time,
    parse_cursor_since,
)
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.runner import FederationRunner
from core.time import utcnow
from tests.unit._acking_queue import AckingQueue

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 28, 12, 0, 0)


def _alert(n: int, when: datetime) -> Dict[str, Any]:
    return {"id": f"a{n}", "t": when}


class _WindowService:
    """A source with a fixed set of alerts, read the way the real ones are.

    ``fetch_alerts`` honours the inclusive ``start_time`` filter, ``limit`` and
    ``oldest_first`` exactly as the four cloud services do after this change.
    """

    def __init__(self, alerts: List[Dict[str, Any]]):
        self.alerts = alerts
        self.calls: List[Dict[str, Any]] = []

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        self.calls.append(
            {"start_time": start_time, "limit": limit, "oldest_first": oldest_first}
        )
        window = [a for a in self.alerts if start_time is None or a["t"] >= start_time]
        window.sort(key=lambda a: a["t"], reverse=not oldest_first)
        return window[:limit]

    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "finding_id": f"w-{alert['id']}",
            "external_id": alert["id"],
            "severity": "high",
            "data_source": "w",
        }


def _adapter(
    service: _WindowService, monkeypatch, *, with_time_reader: bool = True
) -> SIEMIngestionAdapter:
    adapter = SIEMIngestionAdapter(
        name="w",
        integration_id="w",
        default_interval=60,
        service_factory=lambda: service,
        external_id_prefix="w",
        alert_time=(lambda a: a["t"]) if with_time_reader else None,
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    return adapter


# ---------------------------------------------------------------------------
# Adapter cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_batch_stops_the_cursor_at_the_newest_returned_alert(monkeypatch):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)

    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert svc.calls[0]["oldest_first"] is True
    assert [f["external_id"] for f in result.findings] == ["a1", "a2", "a3"]
    # Not now: the two alerts past the batch are still unread.
    assert parse_cursor_since(result.cursor) == T0 + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_next_tick_starts_at_that_time_and_returns_the_remainder(monkeypatch):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)

    first = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)
    second = await adapter.fetch(since=None, cursor=first.cursor, max_items=3)

    assert svc.calls[1]["start_time"] == T0 + timedelta(minutes=3)
    # The boundary alert a3 comes back (start is inclusive) and is left to the
    # runner's dedup and the unique index; a4 and a5 are the remainder.
    assert [f["external_id"] for f in second.findings] == ["a3", "a4", "a5"]


@pytest.mark.asyncio
async def test_short_batch_still_advances_the_cursor_to_now(monkeypatch):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 3)])
    adapter = _adapter(svc, monkeypatch)

    before = utcnow()
    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert len(result.findings) == 2
    assert parse_cursor_since(result.cursor) >= before


@pytest.mark.asyncio
async def test_full_batch_not_past_the_tick_start_steps_the_cursor_forward(
    monkeypatch, caplog
):
    """More than max_items alerts at one instant: do not re-read that page forever."""
    svc = _WindowService([_alert(i, T0) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)

    with caplog.at_level("WARNING"):
        result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    # One millisecond, not one microsecond: Elastic date fields and Security
    # Hub CreatedAt resolve to the millisecond, so a finer step would round
    # back to the same instant on the source and re-read the page every tick.
    assert parse_cursor_since(result.cursor) == T0 + timedelta(milliseconds=1)
    assert "not past the tick start" in caplog.text
    assert "beyond this batch are skipped" in caplog.text


@pytest.mark.asyncio
async def test_a_future_stamped_alert_does_not_move_the_cursor_past_now(
    monkeypatch, caplog
):
    """Clock skew: only Elastic's query has an upper bound of its own, so a full
    batch can hold an alert stamped ahead of this host's clock. The cursor must
    stop at the clock (taken before the fetch), as the pre-change code always
    did, or later alerts with earlier times would be skipped."""
    now = T0 + timedelta(minutes=5)
    svc = _WindowService(
        [
            _alert(1, T0 + timedelta(minutes=1)),
            _alert(2, T0 + timedelta(minutes=2)),
            _alert(3, T0 + timedelta(hours=1)),
        ]
    )
    adapter = _adapter(svc, monkeypatch)
    monkeypatch.setattr("core.federation.adapters._siem_base.utcnow", lambda: now)

    with caplog.at_level("WARNING"):
        result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert len(result.findings) == 3
    assert parse_cursor_since(result.cursor) == now
    assert "ahead of this host's clock" in caplog.text


@pytest.mark.asyncio
async def test_an_aware_time_from_a_reader_is_compared_as_naive_utc(monkeypatch):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)
    adapter._alert_time = lambda a: a["t"].replace(tzinfo=timezone.utc)

    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)
    assert parse_cursor_since(result.cursor) == T0 + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_full_batch_without_a_time_reader_falls_back_to_now(monkeypatch):
    """An adapter built without alert_time keeps the old cursor behaviour."""
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch, with_time_reader=False)

    before = utcnow()
    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert parse_cursor_since(result.cursor) >= before


@pytest.mark.asyncio
async def test_full_batch_with_unreadable_times_falls_back_to_now(monkeypatch, caplog):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)
    adapter._alert_time = lambda a: None

    before = utcnow()
    with caplog.at_level("WARNING"):
        result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert parse_cursor_since(result.cursor) >= before
    assert "no alert carried a readable time" in caplog.text


@pytest.mark.asyncio
async def test_a_reader_that_raises_on_one_record_does_not_fail_the_poll(monkeypatch):
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)

    def flaky(alert):
        if alert["id"] == "a2":
            raise KeyError("t")
        return alert["t"]

    adapter._alert_time = flaky
    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=3)
    assert parse_cursor_since(result.cursor) == T0 + timedelta(minutes=3)


# ---------------------------------------------------------------------------
# Through the runner
# ---------------------------------------------------------------------------


class _FakeDedup:
    def __init__(self):
        self.processed: set = set()

    async def is_processed(self, key: str) -> bool:
        return key in self.processed

    async def mark_processed(self, key: str) -> None:
        self.processed.add(key)

    async def are_processed(self, keys) -> set:
        return {k for k in keys if k in self.processed}

    async def mark_many(self, keys) -> None:
        self.processed.update(keys)


@pytest.mark.asyncio
async def test_runner_persists_the_newest_time_and_the_next_tick_drains(monkeypatch):
    queue: asyncio.Queue = AckingQueue()
    runner = FederationRunner(output_queue=queue)
    svc = _WindowService([_alert(i, T0 + timedelta(minutes=i)) for i in range(1, 6)])
    adapter = _adapter(svc, monkeypatch)
    runner._adapters[adapter.name] = adapter
    runner._dedup[adapter.name] = _FakeDedup()  # type: ignore[assignment]

    stored: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, *, cursor, **_: stored.append(cursor),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda *a, **k: pytest.fail("record_failure should not be called"),
    )

    row = {"max_items": 3, "cursor": cursor_at(T0), "min_severity": None}
    await runner._do_one_tick(adapter, row)
    assert parse_cursor_since(stored[0]) == T0 + timedelta(minutes=3)

    # The next tick reads the stored cursor, as the runner does from the row.
    await runner._do_one_tick(adapter, {**row, "cursor": stored[0]})
    assert svc.calls[1]["start_time"] == T0 + timedelta(minutes=3)
    # a3 a4 a5 fills max_items again, so the cursor stops at a5's time.
    assert parse_cursor_since(stored[1]) == T0 + timedelta(minutes=5)

    # Only the boundary alert is left: a short batch, so the cursor moves to now.
    before = utcnow()
    await runner._do_one_tick(adapter, {**row, "cursor": stored[1]})
    assert parse_cursor_since(stored[2]) >= before

    enqueued = []
    while not queue.empty():
        enqueued.append(queue.get_nowait()["data"]["external_id"])
    # Every alert once: the boundary re-reads (a3, a5) were deduped.
    assert enqueued == ["a1", "a2", "a3", "a4", "a5"]


# ---------------------------------------------------------------------------
# parse_alert_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-09-28T12:00:00Z", T0),
        ("2026-09-28T12:00:00.000Z", T0),
        # Defender: seven fractional digits.
        ("2026-09-28T12:00:00.1234567Z", T0.replace(microsecond=123456)),
        # Sentinel: isoformat() of an aware datetime.
        ("2026-09-28T14:00:00+02:00", T0),
        (datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc), T0),
        (T0, T0),
        ("", None),
        ("not a time", None),
        (None, None),
        (1234567890, None),
    ],
)
def test_parse_alert_time(raw, expected):
    assert parse_alert_time(raw) == expected
