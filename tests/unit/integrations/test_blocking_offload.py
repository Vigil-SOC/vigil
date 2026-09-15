"""Regression tests: integration calls must not stall the event loop.

The integration clients are synchronous by design (#461: plain `def` +
threadpool, `asyncio.to_thread` as the exception). The bug these guard
against is an `async def` calling one directly, which freezes every other
task on that loop for the duration of a remote call — and
`SplunkService.search` polls its job with `time.sleep` for up to ~60s.

Each test runs the async path alongside a 10ms ticker. If the blocking call
is offloaded the ticker keeps advancing; if it is awaited on the loop the
ticker is starved and the assertion fails.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# How long the stubbed remote call blocks for, and the floor on ticks we
# expect to land during it. A loop-blocking implementation yields 0-1.
BLOCK_SECONDS = 0.25
MIN_TICKS = 5


async def _tick_while(coro):
    """Run `coro` while counting 10ms ticks that the loop manages to serve."""
    ticks = 0
    task = asyncio.ensure_future(coro)
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    return await task, ticks


def _blocking(return_value):
    """A stand-in for a slow synchronous remote call."""

    def _call(*args, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return return_value

    return _call


@pytest.mark.asyncio
async def test_splunk_federation_adapter_does_not_block_the_loop():
    from core.integrations.splunk.adapter import _factory

    adapter = _factory()
    svc = MagicMock()
    svc.search = _blocking([])

    with patch.object(adapter, "_get_service", return_value=svc):
        _, ticks = await _tick_while(
            adapter.fetch(since=None, cursor={}, max_items=10)
        )

    assert ticks >= MIN_TICKS, (
        f"loop served only {ticks} ticks during a {BLOCK_SECONDS}s Splunk "
        "search — the call is running on the event loop"
    )


@pytest.mark.asyncio
async def test_crowdstrike_federation_adapter_does_not_block_the_loop():
    from core.integrations.crowdstrike.adapter import _factory

    adapter = _factory()
    svc = MagicMock()
    svc.get_detections = _blocking([])

    with patch.object(adapter, "_get_service", return_value=svc):
        _, ticks = await _tick_while(
            adapter.fetch(since=None, cursor={}, max_items=10)
        )

    assert ticks >= MIN_TICKS, (
        f"loop served only {ticks} ticks during a {BLOCK_SECONDS}s "
        "CrowdStrike fetch — the call is running on the event loop"
    )


@pytest.mark.asyncio
async def test_defender_fetch_alerts_does_not_block_the_loop():
    from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion

    with patch(
        "core.integrations.microsoft_defender.ingestion.get_integration_config",
        return_value={},
    ):
        svc = MicrosoftDefenderIngestion()

    response = MagicMock()
    response.json.return_value = {"value": []}
    response.raise_for_status.return_value = None

    with patch.object(
        svc, "_get_access_token", _blocking("tok-1")
    ), patch(
        "core.integrations.microsoft_defender.ingestion.httpx.get", _blocking(response)
    ):
        _, ticks = await _tick_while(svc.fetch_alerts(limit=10))

    # Two blocking hops here (token exchange + alert fetch), so the budget
    # is 2 * BLOCK_SECONDS.
    assert ticks >= MIN_TICKS, (
        f"loop served only {ticks} ticks during the Defender fetch — a "
        "blocking call is running on the event loop"
    )
