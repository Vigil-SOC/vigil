"""Lag per federation source, and the tick's "more available" report."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from core.federation import store
from core.federation.registry import FetchResult
from core.federation.runner import FederationRunner
from core.time import utcnow
from services.api.routers import federation as federation_router

NOW = datetime(2026, 9, 28, 12, 0, 0)


def _row(**overrides):
    row = {
        "source_id": "fake",
        "enabled": True,
        "cursor": {"last_poll_at": (NOW - timedelta(minutes=4)).isoformat()},
    }
    row.update(overrides)
    return row


def test_lag_of_a_polling_source_is_now_minus_its_cursor():
    assert store.source_lag_seconds(_row(), global_enabled=True, now=NOW) == 240.0


def test_lag_grows_while_the_cursor_holds():
    stuck = _row(cursor={"last_poll_at": (NOW - timedelta(hours=2)).isoformat()})
    assert store.source_lag_seconds(stuck, global_enabled=True, now=NOW) == 7200.0


def test_a_disabled_source_has_no_lag():
    assert (
        store.source_lag_seconds(_row(enabled=False), global_enabled=True, now=NOW)
        is None
    )


def test_no_source_has_lag_while_federation_is_off():
    assert store.source_lag_seconds(_row(), global_enabled=False, now=NOW) is None


def test_a_source_that_never_completed_a_fetch_has_no_lag():
    assert (
        store.source_lag_seconds(_row(cursor={}), global_enabled=True, now=NOW) is None
    )
    assert (
        store.source_lag_seconds(_row(cursor=None), global_enabled=True, now=NOW)
        is None
    )


def test_a_cursor_ahead_of_the_clock_reads_negative():
    ahead = _row(cursor={"last_poll_at": (NOW + timedelta(minutes=10)).isoformat()})
    assert store.source_lag_seconds(ahead, global_enabled=True, now=NOW) == -600.0


@pytest.mark.asyncio
async def test_sources_listing_carries_lag(monkeypatch):
    class _Adapter:
        name = "fake"

        def is_configured(self):
            return True

        def default_interval(self):
            return 300

    polling = _row(
        cursor={"last_poll_at": (utcnow() - timedelta(minutes=3)).isoformat()}
    )
    monkeypatch.setattr(federation_router.fed_store, "list_sources", lambda: [polling])
    monkeypatch.setattr(
        federation_router.fed_store, "get_global_settings", lambda: {"enabled": True}
    )
    monkeypatch.setattr(
        federation_router.fed_registry, "list_adapters", lambda: [_Adapter()]
    )
    monkeypatch.setattr(
        federation_router.fed_registry, "get_adapter", lambda _id: _Adapter()
    )

    listing = await federation_router.list_sources()

    lag = listing["sources"][0]["lag_seconds"]
    assert 170 <= lag <= 190
    assert listing["sources"][0]["settling_margin_seconds"] == 60.0


class _Adapter:
    name = "fake"

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def fetch(self, *, since, cursor, max_items):
        if self._error:
            raise self._error
        return self._result


class _Dedup:
    def __init__(self):
        self.seen = set()

    async def is_processed(self, key):
        return key in self.seen

    async def mark_processed(self, key):
        self.seen.add(key)


def _runner(monkeypatch, adapter):
    runner = FederationRunner(output_queue=asyncio.Queue())
    runner._dedup[adapter.name] = _Dedup()  # type: ignore[assignment]
    monkeypatch.setattr(
        "core.federation.runner.store.record_success", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure", lambda *a, **k: None
    )
    return runner


_ROW = {"max_items": 1, "cursor": {}, "min_severity": None}
_FINDING = {"finding_id": "f-1", "external_id": "e-1", "severity": "high"}


@pytest.mark.asyncio
async def test_a_full_page_reports_more_available(monkeypatch):
    adapter = _Adapter(FetchResult(findings=[_FINDING], cursor={}, truncated=True))
    assert await _runner(monkeypatch, adapter)._do_one_tick(adapter, _ROW) is True


@pytest.mark.asyncio
async def test_a_short_page_does_not_report_more(monkeypatch):
    adapter = _Adapter(FetchResult(findings=[_FINDING], cursor={}))
    assert await _runner(monkeypatch, adapter)._do_one_tick(adapter, _ROW) is False


@pytest.mark.asyncio
async def test_a_failed_fetch_does_not_report_more(monkeypatch):
    adapter = _Adapter(error=RuntimeError("source down"))
    assert await _runner(monkeypatch, adapter)._do_one_tick(adapter, _ROW) is False
