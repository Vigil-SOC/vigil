"""Unit tests for federated monitoring (daemon.federation.*).

These cover the pure-Python pieces of the MVP — adapter contract, registry,
severity floor, cursor parsing, and the runner's tick logic with a fake
adapter. DB/Redis I/O is mocked; integration tests against a live database
live elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from daemon.federation import registry as fed_registry
from daemon.federation.adapters._base import fresh_cursor, parse_cursor_since
from daemon.federation.runner import FederationRunner, _severity_passes


# ---------------------------------------------------------------------------
# Adapter contract / registry
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal adapter used to exercise the runner without external services."""

    name = "fake"

    def __init__(self, *, configured: bool = True, default_interval: int = 30):
        self._configured = configured
        self._default_interval = default_interval
        self.fetch_calls: List[Dict[str, Any]] = []
        self.next_findings: List[Dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self._configured

    def default_interval(self) -> int:
        return self._default_interval

    async def fetch(self, *, since, cursor, max_items):
        self.fetch_calls.append({"since": since, "cursor": cursor, "max_items": max_items})
        from daemon.federation.registry import FetchResult

        return FetchResult(findings=list(self.next_findings), cursor={"tick": len(self.fetch_calls)})


def test_register_and_lookup_adapter():
    fed_registry.register_adapter("fake-test-1", lambda: _FakeAdapter())
    a = fed_registry.get_adapter("fake-test-1")
    assert a is not None
    assert a.name == "fake"
    assert a.is_configured() is True
    assert a.default_interval() == 30


def test_get_adapter_unknown_returns_none():
    assert fed_registry.get_adapter("does-not-exist-xyz") is None


# ---------------------------------------------------------------------------
# Severity floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "finding_sev,floor,expected",
    [
        ("low", None, True),
        ("low", "", True),
        ("low", "low", True),
        ("low", "medium", False),
        ("medium", "medium", True),
        ("medium", "high", False),
        ("high", "high", True),
        ("critical", "high", True),
        ("CRITICAL", "high", True),  # case insensitive
        (None, "low", False),  # unknown severity fails closed
        ("", "medium", False),
    ],
)
def test_severity_passes(finding_sev, floor, expected):
    assert _severity_passes(finding_sev, floor) is expected


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_parse_cursor_since_empty():
    assert parse_cursor_since({}) is None
    assert parse_cursor_since({"foo": "bar"}) is None


def test_parse_cursor_since_iso():
    cursor = {"last_poll_at": "2026-05-04T12:00:00"}
    out = parse_cursor_since(cursor)
    assert isinstance(out, datetime)
    assert out.year == 2026 and out.month == 5 and out.day == 4


def test_parse_cursor_since_iso_with_z():
    cursor = {"last_poll_at": "2026-05-04T12:00:00Z"}
    out = parse_cursor_since(cursor)
    assert isinstance(out, datetime)


def test_fresh_cursor_has_iso_timestamp():
    c = fresh_cursor()
    assert "last_poll_at" in c
    # Round-trip through parse to confirm the format is what we read back
    assert parse_cursor_since(c) is not None


# ---------------------------------------------------------------------------
# Runner tick: respects global toggle, severity floor, dedup, cursor write
# ---------------------------------------------------------------------------


class _FakeDedup:
    def __init__(self):
        self.processed: set = set()

    async def is_processed(self, key: str) -> bool:
        return key in self.processed

    async def mark_processed(self, key: str) -> None:
        self.processed.add(key)


@pytest.mark.asyncio
async def test_runner_do_one_tick_filters_by_severity(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    fake = _FakeAdapter()
    fake.next_findings = [
        {
            "finding_id": "f-1",
            "external_id": "ext-1",
            "severity": "low",
            "data_source": "fake",
        },
        {
            "finding_id": "f-2",
            "external_id": "ext-2",
            "severity": "high",
            "data_source": "fake",
        },
    ]
    runner._adapters[fake.name] = fake
    runner._dedup[fake.name] = _FakeDedup()  # type: ignore[assignment]

    record_success_calls = []
    monkeypatch.setattr(
        "daemon.federation.runner.store.record_success",
        lambda source_id, *, cursor: record_success_calls.append((source_id, cursor)),
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.record_failure",
        lambda *args, **kwargs: pytest.fail("record_failure should not be called"),
    )

    # Floor "high" should drop the "low" finding.
    row = {"max_items": 100, "cursor": {}, "min_severity": "high"}
    await runner._do_one_tick(fake, row)

    enqueued: List[Dict[str, Any]] = []
    while not queue.empty():
        enqueued.append(queue.get_nowait())

    assert len(enqueued) == 1
    assert enqueued[0]["data"]["external_id"] == "ext-2"
    assert record_success_calls == [("fake", {"tick": 1})]


@pytest.mark.asyncio
async def test_runner_do_one_tick_dedups(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    fake = _FakeAdapter()
    fake.next_findings = [
        {"finding_id": "f-1", "external_id": "ext-1", "severity": "high", "data_source": "fake"},
        {"finding_id": "f-1", "external_id": "ext-1", "severity": "high", "data_source": "fake"},
    ]
    runner._adapters[fake.name] = fake
    runner._dedup[fake.name] = _FakeDedup()  # type: ignore[assignment]

    monkeypatch.setattr(
        "daemon.federation.runner.store.record_success", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.record_failure", lambda *a, **k: None
    )

    await runner._do_one_tick(fake, {"max_items": 100, "cursor": {}, "min_severity": None})
    enqueued = []
    while not queue.empty():
        enqueued.append(queue.get_nowait())
    assert len(enqueued) == 1, "Duplicate external_id should be deduplicated"


@pytest.mark.asyncio
async def test_runner_do_one_tick_records_failure(monkeypatch):
    runner = FederationRunner(output_queue=asyncio.Queue())

    class _RaisingAdapter(_FakeAdapter):
        async def fetch(self, **kwargs):
            raise RuntimeError("boom")

    bad = _RaisingAdapter()
    runner._adapters[bad.name] = bad
    runner._dedup[bad.name] = _FakeDedup()  # type: ignore[assignment]

    failures = []
    monkeypatch.setattr(
        "daemon.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )

    await runner._do_one_tick(bad, {"max_items": 100, "cursor": {}, "min_severity": None})
    assert len(failures) == 1
    assert failures[0][0] == "fake"
    assert "boom" in failures[0][1]
    assert runner.stats["errors"] == 1


# ---------------------------------------------------------------------------
# is_active_for: depends on global toggle AND per-source row enabled
# ---------------------------------------------------------------------------


def test_is_active_for_global_off(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "daemon.federation.runner.store.is_globally_enabled", lambda: False
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": True},
    )
    assert runner.is_active_for("splunk") is False


def test_is_active_for_global_on_source_off(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "daemon.federation.runner.store.is_globally_enabled", lambda: True
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": False},
    )
    assert runner.is_active_for("splunk") is False


def test_is_active_for_both_on(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "daemon.federation.runner.store.is_globally_enabled", lambda: True
    )
    monkeypatch.setattr(
        "daemon.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": True},
    )
    assert runner.is_active_for("splunk") is True


# ---------------------------------------------------------------------------
# Seed: only seeds adapters that report is_configured
# ---------------------------------------------------------------------------


def test_seed_only_inserts_configured_adapters(monkeypatch):
    from daemon.federation import seed as fed_seed

    configured = _FakeAdapter(configured=True)
    configured.name = "configured-src"
    unconfigured = _FakeAdapter(configured=False)
    unconfigured.name = "unconfigured-src"

    monkeypatch.setattr(
        "daemon.federation.seed.list_adapters",
        lambda: [configured, unconfigured],
    )

    upserts: List[str] = []
    monkeypatch.setattr(
        "daemon.federation.seed.upsert_source",
        lambda source_id, defaults: (upserts.append(source_id) or {"source_id": source_id}),
    )

    out = fed_seed.seed_federation_sources()
    assert out == ["configured-src"]
    assert upserts == ["configured-src"]
