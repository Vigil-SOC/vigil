"""A legacy Splunk poll with a failed query is a failure, not a clean poll.

``SplunkService.search`` returns ``None`` on error. ``_poll_splunk`` used to
treat that like ``[]`` and return, so ``_poll_splunk_loop`` stamped
``last_poll_time`` and the next tick's fixed lookback skipped the outage.
A query fails if it raised or returned ``None``, and the poll then raises, even
when a later fallback would have run. The last query (the ``notable`` macro,
undefined without ES) may fail once every earlier query ran empty. When ``last_poll_time`` is set, ``earliest_time`` reaches back to it
(capped at 60 minutes).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from services.daemon.config import PollingConfig
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit


class _FakeSplunk:
    """Answers each query in order from ``responses``; an exception is raised."""

    def __init__(self, responses: List[Any], *, on_search=None):
        self.responses = list(responses)
        self.queries: List[Dict[str, Any]] = []
        self._on_search = on_search

    def search(self, **kw) -> Any:
        self.queries.append(kw)
        if self._on_search:
            self._on_search()
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _poller(service: _FakeSplunk) -> DataPoller:
    with (
        patch("services.daemon.poller.FederationRunner"),
        patch("services.daemon.poller.RedisDedupSet") as dedup_cls,
    ):
        poller = DataPoller(PollingConfig())
    dedup = dedup_cls.return_value
    dedup.is_processed = AsyncMock(return_value=False)
    dedup.mark_processed = AsyncMock()
    poller._federation.is_active_for.return_value = False
    poller._splunk_service = service
    poller.set_output_queue(asyncio.Queue())
    return poller


def _event(n: int) -> Dict[str, Any]:
    return {"_cd": f"e{n}", "search_name": f"rule {n}", "urgency": "high"}


@pytest.mark.asyncio
async def test_all_queries_return_none_raises(caplog):
    poller = _poller(_FakeSplunk([None, None, None]))
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError, match="query failed"):
        await poller._poll_splunk()
    assert caplog.text.count("Splunk query failed") >= 1


@pytest.mark.asyncio
async def test_all_queries_raise_raises_with_last_error():
    poller = _poller(
        _FakeSplunk([TimeoutError("read"), TimeoutError("read"), OSError("handshake")])
    )
    with pytest.raises(RuntimeError, match="index=notable.*read"):
        await poller._poll_splunk()


@pytest.mark.asyncio
async def test_empty_first_query_falls_back_and_enqueues():
    svc = _FakeSplunk([[], [_event(1)]])
    poller = _poller(svc)

    await poller._poll_splunk()

    assert len(svc.queries) == 2
    assert poller.stats["splunk_findings"] == 1
    assert poller.stats["errors"] == 0
    item = poller._output_queue.get_nowait()
    assert item["source"] == "splunk"
    assert item["data"]["finding_id"] == "splunk-e1"


@pytest.mark.asyncio
async def test_all_empty_returns_normally():
    svc = _FakeSplunk([[], [], []])
    poller = _poller(svc)

    await poller._poll_splunk()

    assert len(svc.queries) == 3
    assert poller.stats["splunk_findings"] == 0
    assert poller.stats["errors"] == 0
    assert poller._output_queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [[_event(1)], []])
async def test_failed_notable_query_raises_whatever_the_fallback_returns(fallback):
    svc = _FakeSplunk([None, fallback, None])
    poller = _poller(svc)

    with pytest.raises(RuntimeError, match="index=notable"):
        await poller._poll_splunk()

    assert len(svc.queries) == 1
    assert poller._output_queue.empty()


@pytest.mark.asyncio
async def test_failed_macro_query_after_empty_queries_is_a_clean_poll():
    """Non-ES install: no notable index, no alerts, and the macro is undefined."""
    svc = _FakeSplunk([[], [], None])
    poller = _poller(svc)

    await poller._poll_splunk()

    assert len(svc.queries) == 3
    assert poller.stats["errors"] == 0


@pytest.mark.asyncio
async def test_failed_poll_increments_errors_and_leaves_last_poll_time():
    prior = datetime(2026, 9, 30, 11, 0, 0)
    shutdown = asyncio.Event()
    poller = _poller(_FakeSplunk([None, [_event(1)], None], on_search=shutdown.set))
    poller._splunk_state.last_poll_time = prior

    await poller._poll_splunk_loop(shutdown)

    assert poller.stats["errors"] == 1
    assert poller._splunk_state.last_poll_time == prior


@pytest.mark.asyncio
async def test_earliest_time_reaches_back_to_last_success_capped_at_60():
    fixed = datetime(2026, 9, 30, 12, 0, 0)
    svc = _FakeSplunk([[], [], []])
    poller = _poller(svc)
    poller._splunk_state.last_poll_time = fixed - timedelta(minutes=25)

    with patch("services.daemon.poller.utcnow", return_value=fixed):
        await poller._poll_splunk()
    assert {q["earliest_time"] for q in svc.queries} == {"-26m"}

    svc_long = _FakeSplunk([[], [], []])
    poller._splunk_service = svc_long
    poller._splunk_state.last_poll_time = fixed - timedelta(minutes=90)
    with patch("services.daemon.poller.utcnow", return_value=fixed):
        await poller._poll_splunk()
    assert svc_long.queries[0]["earliest_time"] == "-60m"


@pytest.mark.asyncio
async def test_first_run_keeps_fixed_lookback():
    svc = _FakeSplunk([[], [], []])
    poller = _poller(svc)

    await poller._poll_splunk()

    # Default interval 300s -> max(300 // 60 + 1, 5) == 6.
    assert svc.queries[0]["earliest_time"] == "-6m"
