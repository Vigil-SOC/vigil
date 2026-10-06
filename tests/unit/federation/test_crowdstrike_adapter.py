"""A failed CrowdStrike detections query is a failure, not an empty poll (#1433).

``CrowdStrikeService.get_detections`` returns ``None`` on any error, and
``CrowdStrikeAdapter.fetch`` used to turn that (and raised errors) into an
empty result, so an outage was recorded as a healthy poll and the cursor
skipped the window. ``fetch`` now raises, so the runner records a failure and
keeps the cursor. An unconfigured adapter still returns an empty result.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest
import respx

from core.federation.adapters._base import parse_cursor_since
from core.federation.runner import FederationRunner
from core.integrations.crowdstrike.adapter import CrowdStrikeAdapter
from core.integrations.crowdstrike.client import CrowdStrikeService

pytestmark = pytest.mark.unit

BASE = "https://api.crowdstrike.com"
CURSOR = {"last_poll_at": "2026-09-29T02:00:00"}


class _FakeFalcon:
    """Returns ``response`` from get_detections; an exception is raised."""

    def __init__(self, response: Any):
        self.response = response

    def get_detections(self, **kw) -> Any:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _adapter(svc: Any) -> CrowdStrikeAdapter:
    adapter = CrowdStrikeAdapter()
    adapter._service = svc
    adapter.is_configured = lambda: True  # type: ignore[method-assign]
    return adapter


def _fetch(svc: Any):
    return asyncio.run(_adapter(svc).fetch(since=None, cursor=CURSOR, max_items=10))


def _detection(n: int) -> Dict[str, Any]:
    return {
        "detection_id": f"ldt:{n}",
        "max_severity_displayname": "High",
        "created_timestamp": "2026-09-29T02:01:00Z",
    }


def test_query_returning_none_raises():
    with pytest.raises(RuntimeError, match="detections query failed"):
        _fetch(_FakeFalcon(None))


def test_query_raising_propagates():
    with pytest.raises(TimeoutError, match="read"):
        _fetch(_FakeFalcon(TimeoutError("read")))


@pytest.mark.parametrize(
    "route, status",
    [
        ("/detects/queries/detects/v1", 500),
        ("/oauth2/token", 401),
        ("/detects/queries/detects/v1", 429),
    ],
)
@respx.mock
def test_falcon_http_errors_raise(route, status):
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            201, json={"access_token": "tok", "expires_in": 1800}
        )
    )
    respx.get(f"{BASE}/detects/queries/detects/v1").mock(
        return_value=httpx.Response(200, json={"resources": []})
    )
    method = respx.post if route == "/oauth2/token" else respx.get
    method(f"{BASE}{route}").mock(return_value=httpx.Response(status))

    with pytest.raises(RuntimeError, match="detections query failed") as exc:
        _fetch(CrowdStrikeService(client_id="cid", client_secret="csec"))
    assert f"HTTP {status}" in str(exc.value)


def test_empty_result_is_a_successful_empty_poll():
    res = _fetch(_FakeFalcon([]))
    assert res.findings == []
    assert res.cursor["last_poll_at"] > CURSOR["last_poll_at"]


def test_detections_become_findings():
    res = _fetch(_FakeFalcon([_detection(1), _detection(2)]))
    assert [f["external_id"] for f in res.findings] == ["ldt:1", "ldt:2"]
    assert res.cursor["last_poll_at"] > CURSOR["last_poll_at"]


def test_not_configured_returns_empty():
    adapter = CrowdStrikeAdapter()
    with patch.object(adapter, "is_configured", return_value=False):
        res = asyncio.run(adapter.fetch(since=None, cursor=CURSOR, max_items=10))
    assert res.findings == []
    assert "last_poll_at" in res.cursor


@pytest.mark.asyncio
async def test_runner_records_failure_and_keeps_cursor(monkeypatch):
    runner = FederationRunner(output_queue=asyncio.Queue())
    adapter = _adapter(_FakeFalcon(None))

    failures: List[Any] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )

    await runner._do_one_tick(
        adapter, {"max_items": 10, "cursor": CURSOR, "min_severity": None}
    )
    assert failures == [("crowdstrike", "CrowdStrike detections query failed")]
    assert runner.stats["errors"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_runner_stores_http_status_in_last_error(monkeypatch):
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            201, json={"access_token": "tok", "expires_in": 1800}
        )
    )
    respx.get(f"{BASE}/detects/queries/detects/v1").mock(
        return_value=httpx.Response(429)
    )
    runner = FederationRunner(output_queue=asyncio.Queue())
    adapter = _adapter(CrowdStrikeService(client_id="cid", client_secret="csec"))

    failures: List[Any] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )

    await runner._do_one_tick(
        adapter, {"max_items": 10, "cursor": CURSOR, "min_severity": None}
    )
    assert len(failures) == 1
    assert "HTTP 429" in failures[0][1]


@pytest.mark.asyncio
async def test_service_build_failure_is_a_failed_tick(monkeypatch):
    runner = FederationRunner(output_queue=asyncio.Queue())
    adapter = CrowdStrikeAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    monkeypatch.setattr(
        "core.integrations.crowdstrike.adapter.resolve",
        lambda _d: (_ for _ in ()).throw(ValueError("secrets store down")),
    )

    failures: List[Any] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.update_cursor",
        lambda *a, **k: pytest.fail("cursor must not advance"),
        raising=False,
    )

    await runner._do_one_tick(
        adapter, {"max_items": 10, "cursor": CURSOR, "min_severity": None}
    )
    assert failures == [("crowdstrike", "secrets store down")]
    assert runner.stats["errors"] == 1


# ---------------------------------------------------------------------------
# Full batch (#1571)
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 29, 2, 0, 0)


def _timed(n: int, when: datetime) -> Dict[str, Any]:
    return {
        "detection_id": f"ldt:{n}",
        "max_severity_displayname": "High",
        "created_timestamp": when.isoformat() + "Z",
    }


def _mock_falcon(detections: List[Dict[str, Any]], summary_sizes: List[int]):
    """A Falcon whose ID query returns IDs in arbitrary (newest-first) order,
    honours the created_timestamp filter, ``limit`` and ``offset``, and
    records the size of each summaries request."""
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            201, json={"access_token": "tok", "expires_in": 1800}
        )
    )
    by_id = {d["detection_id"]: d for d in detections}

    def query(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        floor = re.search(r"'(.+)Z'", q["filter"]).group(1)
        hits = sorted(
            (
                d
                for d in detections
                if d["created_timestamp"].removesuffix("Z") >= floor
            ),
            key=lambda d: d["created_timestamp"],
            reverse=True,
        )
        off, lim = int(q["offset"]), int(q["limit"])
        page = [d["detection_id"] for d in hits[off : off + lim]]
        return httpx.Response(
            200,
            json={
                "resources": page,
                "meta": {"pagination": {"total": len(hits)}},
            },
        )

    def summaries(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        summary_sizes.append(len(ids))
        assert len(ids) <= 1000
        return httpx.Response(200, json={"resources": [by_id[i] for i in ids]})

    respx.get(f"{BASE}/detects/queries/detects/v1").mock(side_effect=query)
    respx.post(f"{BASE}/detects/entities/summaries/GET/v1").mock(side_effect=summaries)


@respx.mock
def test_full_batch_keeps_the_oldest_and_stops_the_cursor_at_the_newest_kept():
    dets = [_timed(i, T0 + timedelta(minutes=i)) for i in range(1, 6)]
    sizes: List[int] = []
    _mock_falcon(dets, sizes)
    svc = CrowdStrikeService(client_id="cid", client_secret="csec")

    res = asyncio.run(
        _adapter(svc).fetch(
            since=None, cursor={"last_poll_at": T0.isoformat()}, max_items=3
        )
    )

    assert [f["external_id"] for f in res.findings] == ["ldt:1", "ldt:2", "ldt:3"]
    assert parse_cursor_since(res.cursor) == T0 + timedelta(minutes=3)


@respx.mock
def test_more_than_100_ids_are_all_summarised_across_id_pages_and_chunks():
    n = 2500
    dets = [_timed(i, T0 + timedelta(seconds=i)) for i in range(1, n + 1)]
    sizes: List[int] = []
    _mock_falcon(dets, sizes)
    svc = CrowdStrikeService(client_id="cid", client_secret="csec")

    with patch("core.integrations.crowdstrike.client._ID_PAGE_MAX", 1000):
        res = asyncio.run(
            _adapter(svc).fetch(
                since=None,
                cursor={"last_poll_at": T0.isoformat()},
                max_items=n + 1,
            )
        )

    assert sizes == [1000, 1000, 500]
    assert len(res.findings) == n


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
@respx.mock
async def test_runner_delivers_every_detection_across_ticks(monkeypatch):
    dets = [_timed(i, T0 + timedelta(minutes=i)) for i in range(1, 8)]
    _mock_falcon(dets, [])
    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    adapter = _adapter(CrowdStrikeService(client_id="cid", client_secret="csec"))
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

    cursor = {"last_poll_at": T0.isoformat()}
    for _ in range(4):
        row = {"max_items": 3, "cursor": cursor, "min_severity": None}
        await runner._do_one_tick(adapter, row)
        cursor = stored[-1]

    enqueued = []
    while not queue.empty():
        enqueued.append(queue.get_nowait()["data"]["external_id"])
    # Boundary re-reads were deduped: each detection exactly once, in order.
    assert enqueued == [f"ldt:{i}" for i in range(1, 8)]
    # The last tick was short, so the cursor moved to now.
    assert parse_cursor_since(cursor) > T0 + timedelta(minutes=7)
