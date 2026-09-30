"""A Splunk poll in which every query failed is a failure, not an empty poll (#1256).

``SplunkService.search`` returns ``None`` on any error, and ``SplunkAdapter.fetch``
used to treat that (and raised errors) like an empty result, so an outage was
recorded as a healthy poll and the cursor skipped the window. A query now fails
if it raised or returned ``None``; if all fail, ``fetch`` raises. An empty list
still falls through to the next query (non-ES installs have no notable index).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.integrations.splunk.adapter import _QUERIES, SplunkAdapter

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
