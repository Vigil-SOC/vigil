"""A legacy CrowdStrike poll whose query failed is a failure, not an empty poll.

``CrowdStrikeService.get_detections`` returns ``None`` on error and ``[]`` when
nothing is new. ``_poll_crowdstrike`` used to treat both as "return", so
``_poll_crowdstrike_loop`` stamped ``last_poll_time`` after an outage. ``None``
now raises (with the service's ``last_error``); ``[]`` stays a quiet no-op.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit


class _FakeFalcon:
    def __init__(
        self,
        response: Optional[List[Any]],
        last_error: Optional[str] = None,
        on_query=None,
    ):
        self.response = response
        self.last_error = last_error
        self._on_query = on_query

    def get_detections(self, **kw) -> Optional[List[Any]]:
        if self._on_query:
            self._on_query()
        return self.response


def _poller(service: _FakeFalcon) -> DataPoller:
    with (
        patch("services.daemon.poller.FederationRunner"),
        patch("services.daemon.poller.RedisDedupSet") as dedup_cls,
    ):
        poller = DataPoller(PollingConfig())
    dedup = dedup_cls.return_value
    dedup.is_processed = AsyncMock(return_value=False)
    dedup.mark_processed = AsyncMock()
    poller._federation.is_active_for.return_value = False
    poller._crowdstrike_service = service
    poller.set_output_queue(asyncio.Queue())
    return poller


@pytest.mark.asyncio
async def test_none_raises_with_service_error_detail():
    poller = _poller(_FakeFalcon(None, "detections query: HTTP 500"))
    with pytest.raises(RuntimeError, match="query failed: detections query: HTTP 500"):
        await poller._poll_crowdstrike()


@pytest.mark.asyncio
async def test_failed_poll_increments_errors_and_leaves_last_poll_time():
    prior = datetime(2026, 9, 30, 11, 0, 0)
    shutdown = asyncio.Event()
    poller = _poller(_FakeFalcon(None, on_query=shutdown.set))
    poller._crowdstrike_state.last_poll_time = prior

    await poller._poll_crowdstrike_loop(shutdown)

    assert poller.stats["errors"] == 1
    assert poller._crowdstrike_state.last_poll_time == prior


@pytest.mark.asyncio
async def test_empty_poll_is_clean_and_stamps_last_poll_time():
    prior = datetime(2026, 9, 30, 11, 0, 0)
    shutdown = asyncio.Event()
    poller = _poller(_FakeFalcon([], on_query=shutdown.set))
    poller._crowdstrike_state.last_poll_time = prior

    await poller._poll_crowdstrike_loop(shutdown)

    assert poller.stats["errors"] == 0
    assert poller._crowdstrike_state.last_poll_time > prior
