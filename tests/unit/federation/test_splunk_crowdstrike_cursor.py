"""Splunk and CrowdStrike cursors: oldest first, stop at a full page, raise on failure."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from core.federation.adapters._base import (
    SETTLING_MARGIN,
    cursor_at,
    parse_cursor_since,
)
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.integrations.crowdstrike.adapter import CrowdStrikeAdapter
from core.integrations.splunk.adapter import SplunkAdapter

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 28, 12, 0, 0)
NOW = datetime(2026, 9, 28, 13, 0, 0)


def _epoch(when: datetime) -> int:
    return int(when.replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture
def frozen_now(monkeypatch):
    for module in (
        "core.integrations.splunk.adapter",
        "core.integrations.crowdstrike.adapter",
    ):
        monkeypatch.setattr(f"{module}.utcnow", lambda: NOW)


# ---------------------------------------------------------------------------
# Splunk
# ---------------------------------------------------------------------------


class _Splunk:
    def __init__(self, answers: List[Optional[List[Dict[str, Any]]]]):
        self._answers = list(answers)
        self.calls: List[Dict[str, Any]] = []

    def search(self, *, query, earliest_time, latest_time, max_count):
        self.calls.append(
            {"query": query, "earliest_time": earliest_time, "max_count": max_count}
        )
        answer = self._answers.pop(0)
        if answer is None:
            return None
        return answer + [{"count": str(len(answer)), "vigil_now": str(_epoch(NOW))}]


def _splunk(service: _Splunk) -> SplunkAdapter:
    adapter = SplunkAdapter()
    adapter._service = service
    return adapter


def _event(n: int, indexed: datetime) -> Dict[str, Any]:
    return {"_cd": f"cd{n}", "vigil_indextime": str(_epoch(indexed))}


@pytest.mark.asyncio
async def test_splunk_asks_by_index_time_oldest_first_with_a_late_arrival_window(
    frozen_now,
):
    svc = _Splunk([[_event(1, T0 + timedelta(minutes=1))]])

    await _splunk(svc).fetch(since=None, cursor=cursor_at(T0), max_items=10)

    query = svc.calls[0]["query"]
    assert query.startswith(
        f"_index_earliest={_epoch(T0)} _index_latest=now index=notable "
    )
    assert "| sort 0 vigil_indextime | head 10 |" in query
    # Event time reaches back far enough to include an alert indexed late.
    assert svc.calls[0]["earliest_time"] == str(_epoch(T0 - timedelta(hours=24)))


@pytest.mark.asyncio
async def test_splunk_full_page_stops_at_the_newest_index_time(frozen_now):
    events = [_event(i, T0 + timedelta(minutes=i)) for i in range(1, 4)]
    svc = _Splunk([events])

    result = await _splunk(svc).fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert parse_cursor_since(result.cursor) == T0 + timedelta(minutes=3)
    assert result.truncated is True
    assert [f["external_id"] for f in result.findings] == ["cd1", "cd2", "cd3"]


@pytest.mark.asyncio
async def test_splunk_short_page_drains_to_now_less_the_margin(frozen_now):
    svc = _Splunk([[_event(1, T0 + timedelta(minutes=1))]])

    result = await _splunk(svc).fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert parse_cursor_since(result.cursor) == NOW - SETTLING_MARGIN
    assert result.truncated is False


@pytest.mark.asyncio
async def test_splunk_falls_through_to_the_next_query_when_one_is_empty(frozen_now):
    svc = _Splunk([[], None, [_event(1, T0 + timedelta(minutes=1))]])

    result = await _splunk(svc).fetch(since=None, cursor=cursor_at(T0), max_items=3)

    assert len(svc.calls) == 3
    assert [f["external_id"] for f in result.findings] == ["cd1"]


@pytest.mark.asyncio
async def test_splunk_raises_when_every_query_fails(frozen_now):
    with pytest.raises(RuntimeError):
        await _splunk(_Splunk([None, None, None])).fetch(
            since=None, cursor=cursor_at(T0), max_items=3
        )


@pytest.mark.asyncio
async def test_splunk_quiet_source_is_an_empty_success(frozen_now):
    result = await _splunk(_Splunk([[], [], []])).fetch(
        since=None, cursor=cursor_at(T0), max_items=3
    )

    assert result.findings == []
    assert parse_cursor_since(result.cursor) == NOW - SETTLING_MARGIN


@pytest.mark.asyncio
async def test_splunk_full_page_at_one_second_steps_past_it(frozen_now):
    events = [_event(i, T0) for i in range(3)]

    result = await _splunk(_Splunk([events])).fetch(
        since=None, cursor=cursor_at(T0), max_items=3
    )

    assert parse_cursor_since(result.cursor) == T0 + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# CrowdStrike
# ---------------------------------------------------------------------------


class _CrowdStrike:
    def __init__(self, answer: Optional[List[Dict[str, Any]]]):
        self._answer = answer
        self.calls: List[Dict[str, Any]] = []

    def get_detections(self, *, filter_query, limit, sort):
        self.calls.append({"filter": filter_query, "limit": limit, "sort": sort})
        return self._answer


def _crowdstrike(service: _CrowdStrike) -> CrowdStrikeAdapter:
    adapter = CrowdStrikeAdapter()
    adapter._service = service
    return adapter


def _detection(n: int, created: datetime) -> Dict[str, Any]:
    return {"detection_id": f"d{n}", "created_timestamp": created.isoformat() + "Z"}


@pytest.mark.asyncio
async def test_crowdstrike_asks_oldest_first_and_orders_the_details(frozen_now):
    # The details call returns detections in no particular order.
    unordered = [_detection(i, T0 + timedelta(minutes=i)) for i in (3, 1, 2)]
    svc = _CrowdStrike(unordered)

    result = await _crowdstrike(svc).fetch(
        since=None, cursor=cursor_at(T0), max_items=3
    )

    assert svc.calls[0]["sort"] == "created_timestamp|asc"
    assert [f["external_id"] for f in result.findings] == ["d1", "d2", "d3"]
    assert parse_cursor_since(result.cursor) == T0 + timedelta(minutes=3)
    assert result.truncated is True


@pytest.mark.asyncio
async def test_crowdstrike_asks_for_no_more_than_one_details_batch(frozen_now):
    svc = _CrowdStrike([])

    await _crowdstrike(svc).fetch(since=None, cursor=cursor_at(T0), max_items=500)

    assert svc.calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_crowdstrike_short_page_drains_to_now_less_the_margin(frozen_now):
    svc = _CrowdStrike([_detection(1, T0 + timedelta(minutes=1))])

    result = await _crowdstrike(svc).fetch(
        since=None, cursor=cursor_at(T0), max_items=3
    )

    assert parse_cursor_since(result.cursor) == NOW - SETTLING_MARGIN
    assert result.truncated is False


@pytest.mark.asyncio
async def test_crowdstrike_raises_when_the_query_fails(frozen_now):
    with pytest.raises(RuntimeError):
        await _crowdstrike(_CrowdStrike(None)).fetch(
            since=None, cursor=cursor_at(T0), max_items=3
        )


# ---------------------------------------------------------------------------
# SIEM adapter base: "more available"
# ---------------------------------------------------------------------------


class _Siem:
    def __init__(self, alerts):
        self.alerts = alerts

    async def fetch_alerts(self, start_time=None, limit=100, oldest_first=False):
        return self.alerts[:limit]

    def transform_alert_to_finding(self, alert):
        return {"finding_id": f"s-{alert['id']}", "external_id": alert["id"]}


def _siem(alerts, *, with_reader=True) -> SIEMIngestionAdapter:
    adapter = SIEMIngestionAdapter(
        name="s",
        integration_id="s",
        default_interval=60,
        service_factory=lambda: _Siem(alerts),
        external_id_prefix="s",
        alert_time=(lambda a: a["t"]) if with_reader else None,
    )
    adapter.is_configured = lambda: True  # type: ignore[method-assign]
    return adapter


_ALERTS = [{"id": f"a{i}", "t": T0 + timedelta(minutes=i)} for i in range(1, 4)]


@pytest.mark.asyncio
async def test_siem_full_page_reports_more_available():
    result = await _siem(_ALERTS).fetch(since=None, cursor=cursor_at(T0), max_items=3)
    assert result.truncated is True


@pytest.mark.asyncio
async def test_siem_short_page_does_not_report_more():
    result = await _siem(_ALERTS).fetch(since=None, cursor=cursor_at(T0), max_items=5)
    assert result.truncated is False


@pytest.mark.asyncio
async def test_siem_without_a_time_reader_never_reports_more():
    # Its cursor goes to now, so fetching again soon would not reach the rest.
    result = await _siem(_ALERTS, with_reader=False).fetch(
        since=None, cursor=cursor_at(T0), max_items=3
    )
    assert result.truncated is False
