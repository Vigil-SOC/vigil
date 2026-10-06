"""A failed CrowdStrike detections query is a failure, not an empty poll (#1433).

``CrowdStrikeService.get_detections`` returns ``None`` on any error, and
``CrowdStrikeAdapter.fetch`` used to turn that (and raised errors) into an
empty result, so an outage was recorded as a healthy poll and the cursor
skipped the window. ``fetch`` now raises, so the runner records a failure and
keeps the cursor. An unconfigured adapter still returns an empty result.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest
import respx

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
