"""A Splunk poll in which every query failed is a failure, not an empty poll (#1256).

``SplunkService.search`` returns ``None`` on any error, and ``SplunkAdapter.fetch``
used to treat that (and raised errors) like an empty result, so an outage was
recorded as a healthy poll and the cursor skipped the window. A query now fails
if it raised or returned ``None``; if all fail, ``fetch`` raises. An empty list
still falls through to the next query (non-ES installs have no notable index).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.federation.adapters._base import parse_cursor_since
from core.integrations.splunk.adapter import _QUERIES, SplunkAdapter
from core.time import utcnow

pytestmark = pytest.mark.unit

CURSOR = {"last_poll_at": "2026-09-29T02:00:00"}


class _FakeSplunk:
    """Answers each query in order from ``responses``; an exception is raised."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.queries: List[str] = []

    def search(self, **kw) -> Any:
        self.queries.append(kw["query"])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _fetch(responses: List[Any]):
    adapter = SplunkAdapter()
    svc = _FakeSplunk(responses)
    adapter._service = svc
    return asyncio.run(adapter.fetch(since=None, cursor=CURSOR, max_items=10)), svc


def _event(n: int) -> Dict[str, Any]:
    return {"_cd": f"e{n}", "search_name": f"rule {n}", "urgency": "high"}


def test_all_queries_return_none_raises():
    with pytest.raises(RuntimeError, match="every search query failed"):
        _fetch([None, None, None])


def test_all_queries_raise_raises_with_last_error():
    with pytest.raises(RuntimeError, match="handshake"):
        _fetch([TimeoutError("read"), TimeoutError("read"), OSError("handshake")])


def test_empty_first_query_falls_back_to_next():
    res, svc = _fetch([[], [_event(1)], None])
    assert [f["external_id"] for f in res.findings] == ["e1"]
    assert len(svc.queries) == 2


def test_all_empty_is_a_successful_empty_poll():
    res, svc = _fetch([[], [], []])
    assert res.findings == []
    assert res.cursor["last_poll_at"] > CURSOR["last_poll_at"]
    assert len(svc.queries) == len(_QUERIES)


def test_one_query_running_empty_is_success_despite_others_failing():
    res, _ = _fetch([None, [], None])
    assert res.findings == []


def test_service_construction_failure_raises_and_is_retried():
    adapter = SplunkAdapter()
    with (
        patch.object(adapter, "is_configured", return_value=True),
        patch(
            "core.integrations.splunk.client.SplunkService",
            side_effect=ValueError("bad url"),
        ),
    ):
        with pytest.raises(ValueError, match="bad url"):
            asyncio.run(adapter.fetch(since=None, cursor=CURSOR, max_items=10))
    assert adapter._service is None


def test_not_configured_returns_empty():
    adapter = SplunkAdapter()
    with patch.object(adapter, "is_configured", return_value=False):
        res = asyncio.run(adapter.fetch(since=None, cursor=CURSOR, max_items=10))
    assert res.findings == []
    assert "last_poll_at" in res.cursor


# ---------------------------------------------------------------------------
# Full batch (#1571)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 29, 2, 0, 30)  # mid-minute on purpose


def _timed(n: int, when: datetime) -> Dict[str, Any]:
    return {
        "_cd": f"e{n}",
        "_time": when.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds"),
        "urgency": "high",
    }


class _WindowSplunk:
    """Honours an absolute epoch ``earliest_time``, ``sort 0 _time`` and
    ``head N`` the way Splunk does, over a fixed set of events."""

    def __init__(self, events: List[Dict[str, Any]]):
        self.events = events
        self.earliest: List[str] = []

    def search(self, *, query: str, earliest_time: str, max_count: int, **kw):
        self.earliest.append(earliest_time)
        assert "sort 0 _time" in query
        floor = float(earliest_time)
        hits = [
            e
            for e in self.events
            if datetime.fromisoformat(e["_time"]).timestamp() >= floor
        ]
        return sorted(hits, key=lambda e: e["_time"])[:max_count]


def _window_adapter(events: List[Dict[str, Any]]):
    adapter = SplunkAdapter()
    svc = _WindowSplunk(events)
    adapter._service = svc
    return adapter, svc


def test_full_batch_keeps_the_oldest_and_stops_the_cursor_at_the_newest_kept():
    adapter, svc = _window_adapter(
        [_timed(i, T0 + timedelta(minutes=i)) for i in range(1, 6)]
    )

    res = asyncio.run(
        adapter.fetch(since=None, cursor={"last_poll_at": T0.isoformat()}, max_items=3)
    )

    assert [f["external_id"] for f in res.findings] == ["e1", "e2", "e3"]
    assert parse_cursor_since(res.cursor) == T0 + timedelta(minutes=3)
    # Absolute epoch of the cursor, not a minute-rounded "-Nm".
    assert float(svc.earliest[0]) == T0.replace(tzinfo=timezone.utc).timestamp()


def test_events_in_one_minute_still_progress_the_cursor():
    """A full batch inside one minute must not re-read the same page forever."""
    events = [_timed(i, T0 + timedelta(seconds=i)) for i in range(1, 8)]
    adapter, _ = _window_adapter(events)

    cursor = {"last_poll_at": T0.isoformat()}
    seen: List[str] = []
    for _ in range(4):
        res = asyncio.run(adapter.fetch(since=None, cursor=cursor, max_items=3))
        seen += [f["external_id"] for f in res.findings]
        cursor = res.cursor

    assert set(seen) == {f"e{i}" for i in range(1, 8)}
    # Ticks 1-3 filled the batch; the cursor advanced each time (e3, e6, e7...).
    assert parse_cursor_since(cursor) > T0 + timedelta(seconds=7)


def test_events_at_one_instant_step_the_cursor_forward(caplog):
    adapter, _ = _window_adapter([_timed(i, T0) for i in range(1, 6)])

    with caplog.at_level("WARNING"):
        res = asyncio.run(
            adapter.fetch(
                since=None, cursor={"last_poll_at": T0.isoformat()}, max_items=3
            )
        )

    assert parse_cursor_since(res.cursor) == T0 + timedelta(milliseconds=1)
    assert "not past the tick start" in caplog.text


def test_full_batch_with_unreadable_times_warns_and_moves_to_now(caplog):
    events = [{"_cd": f"e{i}", "_time": "garbage"} for i in range(3)]
    adapter = SplunkAdapter()
    adapter._service = _FakeSplunk([events])

    before = utcnow()
    with caplog.at_level("WARNING"):
        res = asyncio.run(adapter.fetch(since=None, cursor=CURSOR, max_items=3))

    assert parse_cursor_since(res.cursor) >= before
    assert "no alert carried a readable time" in caplog.text
