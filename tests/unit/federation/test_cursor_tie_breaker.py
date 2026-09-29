"""A burst sharing one timestamp is paged by alert ID instead of stepped over."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from core.federation.adapters._base import cursor_at, next_cursor, parse_cursor_since
from core.federation.adapters._siem_base import SIEMIngestionAdapter

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 28, 12, 0, 0)
NOW = datetime(2026, 9, 28, 13, 0, 0)
STEP = timedelta(milliseconds=1)


def _next(times, ids=None, *, start=T0, after=None, truncated=True):
    return next_cursor(
        times,
        truncated=truncated,
        start=start,
        now=NOW,
        step=STEP,
        source="t",
        ids=ids,
        after=after,
    )


def test_a_full_batch_stores_the_last_alert_id_beside_its_time():
    cursor, more = _next([T0, T0 + STEP], ["b", "a"])
    assert cursor == {"last_poll_at": (T0 + STEP).isoformat(), "after_id": "a"}
    assert more is True


def test_a_full_batch_at_the_start_instant_pages_on_instead_of_stepping():
    cursor, more = _next([T0, T0, T0], ["a", "b", "c"], after=None)
    assert cursor == {"last_poll_at": T0.isoformat(), "after_id": "c"}
    assert more is True


def test_an_id_that_does_not_advance_falls_back_to_the_step():
    # The source ignored after_id and handed back the same page.
    cursor, _ = _next([T0, T0], ["a", "b"], after="b")
    assert cursor == cursor_at(T0 + STEP)


def test_a_capped_future_time_drops_the_id():
    cursor, more = _next([NOW + timedelta(hours=1)], ["z"])
    assert cursor == cursor_at(NOW)
    assert more is True


def test_a_short_batch_carries_no_id():
    cursor, more = _next([T0], ["a"], truncated=False)
    assert "after_id" not in cursor
    assert more is False


def test_without_ids_a_stuck_instant_still_steps():
    cursor, _ = _next([T0, T0])
    assert cursor == cursor_at(T0 + STEP)


class _Burst:
    """Alerts all at one instant; honours after_id the way Elastic's query does."""

    def __init__(self, ids: List[str]):
        self.alerts = [{"id": i, "t": T0} for i in sorted(ids)]
        self.calls: List[Dict[str, Any]] = []

    async def fetch_alerts(
        self,
        start_time=None,
        limit=100,
        oldest_first=False,
        after_id: Optional[str] = None,
    ):
        self.calls.append({"start_time": start_time, "after_id": after_id})
        window = [
            a
            for a in self.alerts
            if a["t"] > start_time
            or (a["t"] == start_time and a["id"] > (after_id or ""))
        ]
        return window[:limit]

    def transform_alert_to_finding(self, alert):
        return {"finding_id": f"b-{alert['id']}", "external_id": alert["id"]}


@pytest.mark.asyncio
async def test_a_burst_at_one_instant_is_read_in_full_across_ticks(monkeypatch):
    svc = _Burst([f"u{n:02d}" for n in range(7)])
    adapter = SIEMIngestionAdapter(
        name="b",
        integration_id="b",
        default_interval=60,
        service_factory=lambda: svc,
        external_id_prefix="b",
        alert_time=lambda a: a["t"],
        alert_id=lambda a: a["id"],
    )
    adapter.is_configured = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("core.federation.adapters._siem_base.utcnow", lambda: NOW)

    seen: List[str] = []
    cursor = cursor_at(T0)
    for _ in range(4):
        result = await adapter.fetch(since=None, cursor=cursor, max_items=3)
        seen += [f["external_id"] for f in result.findings]
        cursor = result.cursor
        if not result.truncated:
            break

    assert seen == [f"u{n:02d}" for n in range(7)]
    assert [c["after_id"] for c in svc.calls] == [None, "u02", "u05"]
    assert parse_cursor_since(cursor) == NOW - timedelta(minutes=1)
