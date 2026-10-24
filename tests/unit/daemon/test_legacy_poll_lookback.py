"""Legacy CrowdStrike and Elastic polls reach back to their last successful poll.

Both used a fixed ``max(interval // 60 + 1, 5)`` minute window, so after an
outage longer than that the detections created in the gap were never read.
They now share Splunk's lookback: back to ``last_poll_time`` rounded up one
minute, capped at 60, with the fixed window only before the first success.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, 12, 0, 0)


class _Falcon:
    last_error = None

    def __init__(self):
        self.filters: List[str] = []

    def get_detections(self, filter_query=None, **kw) -> List[Any]:
        self.filters.append(filter_query)
        return []

    def since(self) -> datetime:
        return datetime.fromisoformat(self.filters[0].split("'")[1].rstrip("Z"))


class _Elastic:
    def __init__(self):
        self.starts: List[datetime] = []

    async def fetch_alerts(self, start_time=None, limit=100):
        self.starts.append(start_time)
        return []

    def transform_alert_to_finding(self, alert):
        return None


def _poller() -> DataPoller:
    with (
        patch("services.daemon.poller.FederationRunner"),
        patch("services.daemon.poller.RedisDedupSet") as dedup_cls,
    ):
        poller = DataPoller(PollingConfig())
    dedup_cls.return_value.is_processed = AsyncMock(return_value=False)
    dedup_cls.return_value.mark_processed = AsyncMock()
    poller._federation.is_active_for.return_value = False
    poller._crowdstrike_service = _Falcon()
    poller._elastic_service = _Elastic()
    poller.set_output_queue(asyncio.Queue())
    return poller


async def _query_starts(last: Optional[datetime]) -> tuple[datetime, datetime]:
    poller = _poller()
    poller._crowdstrike_state.last_poll_time = last
    poller._elastic_state.last_poll_time = last
    with patch("services.daemon.poller.utcnow", return_value=_NOW):
        await poller._poll_crowdstrike()
        await poller._poll_elastic()
    return poller._crowdstrike_service.since(), poller._elastic_service.starts[0]


@pytest.mark.asyncio
async def test_reaches_back_past_last_success_after_an_outage():
    cs, elastic = await _query_starts(_NOW - timedelta(minutes=30))
    assert cs == elastic == _NOW - timedelta(minutes=31)


@pytest.mark.asyncio
async def test_lookback_caps_at_60_minutes():
    cs, elastic = await _query_starts(_NOW - timedelta(hours=5))
    assert cs == elastic == _NOW - timedelta(minutes=60)


@pytest.mark.asyncio
async def test_first_run_keeps_fixed_window():
    cs, elastic = await _query_starts(None)
    # CrowdStrike 60s -> max(2, 5) == 5; Elastic shares Splunk's 300s -> 6.
    assert cs == _NOW - timedelta(minutes=5)
    assert elastic == _NOW - timedelta(minutes=6)
