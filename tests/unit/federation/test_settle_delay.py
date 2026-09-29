"""The adapter's settle delay keeps late-indexed alerts in view (#1252).

Filebeat indexes a Wazuh alert seconds after its ``@timestamp``. A tick that
read up to now and persisted now would step past an alert still in flight.
With ``settle_delay`` the tick reads up to now - delay and persists exactly
that instant, so the only window state is the persisted cursor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from core.federation.adapters._base import cursor_at, parse_cursor_since
from core.federation.adapters._siem_base import SIEMIngestionAdapter

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 28, 12, 0, 0)
DELAY = timedelta(seconds=60)


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Indexer:
    """Alerts become searchable at ``indexed``, later than their stamp ``t``."""

    def __init__(self, clock: _Clock, alerts: List[Dict[str, Any]]):
        self.clock = clock
        self.alerts = alerts
        self.calls: List[Dict[str, Any]] = []

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        self.calls.append({"start_time": start_time, "end_time": end_time})
        hits = [
            a
            for a in self.alerts
            if a["indexed"] <= self.clock.now
            and a["t"] >= start_time
            and (end_time is None or a["t"] <= end_time)
        ]
        hits.sort(key=lambda a: a["t"])
        return hits[:limit]

    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        return {"finding_id": f"w-{alert['id']}", "external_id": alert["id"]}


def _alert(ident: str, t: datetime, lag: timedelta = timedelta(0)) -> Dict[str, Any]:
    return {"id": ident, "t": t, "indexed": t + lag}


def _adapter(svc, monkeypatch, clock, settle_delay=DELAY) -> SIEMIngestionAdapter:
    monkeypatch.setattr("core.federation.adapters._siem_base.utcnow", clock)
    adapter = SIEMIngestionAdapter(
        name="w",
        integration_id="w",
        default_interval=60,
        service_factory=lambda: svc,
        external_id_prefix="w",
        alert_time=lambda a: a["t"],
        settle_delay=settle_delay,
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    return adapter


@pytest.mark.asyncio
async def test_short_batch_persists_now_minus_delay_and_the_next_tick_starts_there(
    monkeypatch,
):
    clock = _Clock(T0)
    svc = _Indexer(clock, [])
    adapter = _adapter(svc, monkeypatch, clock)

    first = await adapter.fetch(
        since=None, cursor=cursor_at(T0 - timedelta(minutes=5)), max_items=10
    )
    assert svc.calls[0]["end_time"] == T0 - DELAY
    assert parse_cursor_since(first.cursor) == T0 - DELAY

    clock.now = T0 + timedelta(minutes=5)
    await adapter.fetch(since=None, cursor=first.cursor, max_items=10)
    assert svc.calls[1]["start_time"] == T0 - DELAY
    assert svc.calls[1]["end_time"] == clock.now - DELAY


@pytest.mark.asyncio
async def test_an_alert_indexed_after_the_poll_is_read_on_the_next_tick(monkeypatch):
    # Stamped 10 s before the poll, searchable only 20 s after it.
    late = _alert("late", T0 - timedelta(seconds=10), lag=timedelta(seconds=30))
    clock = _Clock(T0)
    svc = _Indexer(clock, [late])
    adapter = _adapter(svc, monkeypatch, clock)

    first = await adapter.fetch(
        since=None, cursor=cursor_at(T0 - timedelta(minutes=5)), max_items=10
    )
    assert first.findings == []

    clock.now = T0 + timedelta(minutes=5)
    second = await adapter.fetch(since=None, cursor=first.cursor, max_items=10)
    assert [f["external_id"] for f in second.findings] == ["late"]


@pytest.mark.asyncio
async def test_without_a_delay_the_same_alert_is_skipped(monkeypatch):
    """The regression the delay exists for: the pre-change cursor is now."""
    late = _alert("late", T0 - timedelta(seconds=10), lag=timedelta(seconds=30))
    clock = _Clock(T0)
    svc = _Indexer(clock, [late])
    adapter = _adapter(svc, monkeypatch, clock, settle_delay=None)
    monkeypatch.setattr("core.federation.adapters._base.utcnow", clock)

    first = await adapter.fetch(
        since=None, cursor=cursor_at(T0 - timedelta(minutes=5)), max_items=10
    )
    assert svc.calls[0]["end_time"] is None
    clock.now = T0 + timedelta(minutes=5)
    second = await adapter.fetch(since=None, cursor=first.cursor, max_items=10)
    assert first.findings == second.findings == []


@pytest.mark.asyncio
async def test_full_batch_stops_at_the_newest_alert_and_drains(monkeypatch):
    clock = _Clock(T0)
    alerts = [_alert(f"a{i}", T0 - timedelta(minutes=5 - i)) for i in range(5)]
    svc = _Indexer(clock, alerts)
    adapter = _adapter(svc, monkeypatch, clock)

    cursor = cursor_at(T0 - timedelta(minutes=6))
    seen: List[str] = []
    for _ in range(10):
        result = await adapter.fetch(since=None, cursor=cursor, max_items=2)
        seen += [f["external_id"] for f in result.findings]
        if len(result.findings) < 2:
            break
        # Full batch: the cursor is the newest returned alert, not the window end.
        assert parse_cursor_since(result.cursor) == max(
            a["t"] for a in alerts if a["id"] in seen
        )
        cursor = result.cursor
    assert sorted(set(seen)) == [f"a{i}" for i in range(5)]
    assert parse_cursor_since(result.cursor) == T0 - DELAY


@pytest.mark.asyncio
async def test_full_batch_cursor_is_capped_at_the_window_end(monkeypatch):
    """A source that ignores ``end_time`` cannot carry the cursor past it."""
    clock = _Clock(T0)
    svc = _Indexer(clock, [_alert("x", T0 - timedelta(seconds=5))])
    honour = svc.fetch_alerts

    async def ignore_end(**kw):
        return await honour(**{**kw, "end_time": None})

    svc.fetch_alerts = ignore_end
    adapter = _adapter(svc, monkeypatch, clock)
    result = await adapter.fetch(
        since=None, cursor=cursor_at(T0 - timedelta(minutes=5)), max_items=1
    )
    assert parse_cursor_since(result.cursor) == T0 - DELAY


@pytest.mark.asyncio
async def test_a_restarted_adapter_resumes_from_the_persisted_cursor(monkeypatch):
    clock = _Clock(T0)
    late = _alert("late", T0 - timedelta(seconds=10), lag=timedelta(seconds=30))
    svc = _Indexer(clock, [late])
    first = await _adapter(svc, monkeypatch, clock).fetch(
        since=None, cursor=cursor_at(T0 - timedelta(minutes=5)), max_items=10
    )

    clock.now = T0 + timedelta(minutes=5)
    fresh = _adapter(_Indexer(clock, [late]), monkeypatch, clock)
    second = await fresh.fetch(since=None, cursor=first.cursor, max_items=10)
    assert [f["external_id"] for f in second.findings] == ["late"]


@pytest.mark.asyncio
async def test_a_cursor_later_than_the_window_end_is_not_moved_back(monkeypatch):
    clock = _Clock(T0)
    svc = _Indexer(clock, [])
    adapter = _adapter(svc, monkeypatch, clock)
    result = await adapter.fetch(since=None, cursor=cursor_at(T0), max_items=10)
    assert parse_cursor_since(result.cursor) == T0


def test_the_elastic_factory_sets_a_60s_delay():
    from core.integrations.elastic.adapter import SETTLE_DELAY, _factory

    assert _factory()._settle_delay == SETTLE_DELAY == timedelta(seconds=60)
