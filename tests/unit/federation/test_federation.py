"""Unit tests for federated monitoring (core.federation.*).

These cover the pure-Python pieces of the MVP — adapter contract, registry,
severity floor, cursor parsing, and the runner's tick logic with a fake
adapter. DB/Redis I/O is mocked; integration tests against a live database
live elsewhere.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from core.federation import registry as fed_registry
from core.federation.adapters._base import fresh_cursor, parse_cursor_since
from core.federation.adapters._siem_base import SIEMIngestionAdapter
from core.federation.runner import FederationRunner, _severity_passes

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
        self.fetch_calls.append(
            {"since": since, "cursor": cursor, "max_items": max_items}
        )
        from core.federation.registry import FetchResult

        return FetchResult(
            findings=list(self.next_findings), cursor={"tick": len(self.fetch_calls)}
        )


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

    async def are_processed(self, keys) -> set:
        return {k for k in keys if k in self.processed}

    async def mark_many(self, keys) -> None:
        self.processed.update(keys)


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
        "core.federation.runner.store.record_success",
        lambda source_id, *, cursor, dropped=0: record_success_calls.append(
            (source_id, cursor)
        ),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
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
        {
            "finding_id": "f-1",
            "external_id": "ext-1",
            "severity": "high",
            "data_source": "fake",
        },
        {
            "finding_id": "f-1",
            "external_id": "ext-1",
            "severity": "high",
            "data_source": "fake",
        },
    ]
    runner._adapters[fake.name] = fake
    runner._dedup[fake.name] = _FakeDedup()  # type: ignore[assignment]

    monkeypatch.setattr(
        "core.federation.runner.store.record_success", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure", lambda *a, **k: None
    )

    await runner._do_one_tick(
        fake, {"max_items": 100, "cursor": {}, "min_severity": None}
    )
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
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )

    await runner._do_one_tick(
        bad, {"max_items": 100, "cursor": {}, "min_severity": None}
    )
    assert len(failures) == 1
    assert failures[0][0] == "fake"
    assert "boom" in failures[0][1]
    assert runner.stats["errors"] == 1


@pytest.mark.asyncio
async def test_siem_adapter_propagates_a_failed_fetch(monkeypatch):
    """Swallowing it would return an empty success and advance the cursor."""
    from core.federation.adapters._siem_base import SIEMIngestionAdapter

    class _DownService:
        async def fetch_alerts(self, **kwargs):
            raise ConnectionError("indexer unreachable")

    adapter = SIEMIngestionAdapter(
        name="down",
        integration_id="down",
        default_interval=300,
        service_factory=_DownService,
        external_id_prefix="down",
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    with pytest.raises(ConnectionError):
        await adapter.fetch(since=None, cursor={}, max_items=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module, ingestion_cls, url_key, service_cls",
    [
        (
            "core.integrations.elastic.ingestion",
            "ElasticIngestion",
            "elasticsearch_url",
            "ElasticService",
        ),
        (
            "core.integrations.opensearch.ingestion",
            "OpenSearchIngestion",
            "opensearch_url",
            "OpenSearchService",
        ),
    ],
)
async def test_client_construction_error_keeps_the_cursor(
    monkeypatch, module, ingestion_cls, url_key, service_cls
):
    """A client that cannot be built is a failed poll: failure recorded and no
    success, so the stored cursor stays put (#1573)."""
    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "resolve", lambda _spec: {url_key: "https://x.test"})
    monkeypatch.setattr(
        mod, service_cls, mock.MagicMock(side_effect=ValueError("bad ca_cert_path"))
    )
    adapter = SIEMIngestionAdapter(
        name="siem",
        integration_id="siem",
        default_interval=60,
        service_factory=getattr(mod, ingestion_cls),
        external_id_prefix="siem",
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    runner = FederationRunner(output_queue=asyncio.Queue())
    failures = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )

    row = {
        "max_items": 100,
        "cursor": {"last_poll_at": "2026-05-04T12:00:00"},
        "min_severity": None,
    }
    await runner._do_one_tick(adapter, row)

    assert len(failures) == 1 and "bad ca_cert_path" in failures[0][1]


@pytest.mark.asyncio
async def test_missing_sdk_records_failure_and_keeps_cursor(monkeypatch):
    """A source whose SDK is not installed fails the tick instead of a quiet success."""
    import sys

    from core.federation.adapters._siem_base import SIEMIngestionAdapter
    from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion

    monkeypatch.setitem(sys.modules, "boto3", None)
    adapter = SIEMIngestionAdapter(
        name="aws_security_hub",
        integration_id="aws_security_hub",
        default_interval=300,
        service_factory=AWSSecurityHubIngestion,
        external_id_prefix="aws-sh",
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    runner = FederationRunner(output_queue=asyncio.Queue())
    runner._adapters[adapter.name] = adapter
    runner._dedup[adapter.name] = _FakeDedup()  # type: ignore[assignment]

    failures = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append((source_id, error)),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success would advance the cursor"),
    )

    await runner._do_one_tick(
        adapter, {"max_items": 100, "cursor": {}, "min_severity": None}
    )
    assert len(failures) == 1
    assert failures[0][0] == "aws_security_hub"
    assert "boto3" in failures[0][1] and "pip install boto3" in failures[0][1]


# ---------------------------------------------------------------------------
# is_active_for: depends on global toggle AND per-source row enabled
# ---------------------------------------------------------------------------


def test_is_active_for_global_off(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "core.federation.runner.store.is_globally_enabled", lambda: False
    )
    monkeypatch.setattr(
        "core.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": True},
    )
    assert runner.is_active_for("splunk") is False


def test_is_active_for_global_on_source_off(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "core.federation.runner.store.is_globally_enabled", lambda: True
    )
    monkeypatch.setattr(
        "core.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": False},
    )
    assert runner.is_active_for("splunk") is False


def test_is_active_for_both_on(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(
        "core.federation.runner.store.is_globally_enabled", lambda: True
    )
    monkeypatch.setattr(
        "core.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": True},
    )
    assert runner.is_active_for("splunk") is True


# ---------------------------------------------------------------------------
# Seed: only seeds adapters that report is_configured
# ---------------------------------------------------------------------------


def test_seed_only_inserts_configured_adapters(monkeypatch):
    from core.federation import seed as fed_seed

    configured = _FakeAdapter(configured=True)
    configured.name = "configured-src"
    unconfigured = _FakeAdapter(configured=False)
    unconfigured.name = "unconfigured-src"

    monkeypatch.setattr(
        "core.federation.seed.list_adapters",
        lambda: [configured, unconfigured],
    )

    upserts: List[str] = []
    monkeypatch.setattr(
        "core.federation.seed.upsert_source",
        lambda source_id, defaults: (
            upserts.append(source_id) or {"source_id": source_id}
        ),
    )

    out = fed_seed.seed_federation_sources()
    assert out == ["configured-src"]
    assert upserts == ["configured-src"]


@pytest.mark.asyncio
async def test_siem_adapter_propagates_a_service_that_cannot_be_built(monkeypatch):
    """A configured source that cannot build its service is failing, not empty."""
    from core.federation.adapters._siem_base import SIEMIngestionAdapter

    def broken_factory():
        raise RuntimeError("secret store unavailable")

    adapter = SIEMIngestionAdapter(
        name="broken",
        integration_id="broken",
        default_interval=300,
        service_factory=broken_factory,
        external_id_prefix="broken",
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    with pytest.raises(RuntimeError, match="secret store"):
        await adapter.fetch(since=None, cursor={}, max_items=10)


# ---------------------------------------------------------------------------
# Dropped-record accounting
# ---------------------------------------------------------------------------


class _LossySvc:
    """Transform raises on 'boom', returns None on 'none', else a finding."""

    async def fetch_alerts(self, start_time=None, limit=100, oldest_first=False):
        return [{"id": i} for i in ("a", "boom", "none", "b")]

    def transform_alert_to_finding(self, alert):
        if alert["id"] == "boom":
            raise ValueError("bad shape")
        if alert["id"] == "none":
            return None
        return {"finding_id": f"x-{alert['id']}"}


@pytest.mark.asyncio
async def test_siem_adapter_counts_dropped_and_warns_once(monkeypatch, caplog):
    from core.federation.adapters._siem_base import SIEMIngestionAdapter

    adapter = SIEMIngestionAdapter(
        name="lossy",
        integration_id="lossy",
        default_interval=60,
        service_factory=_LossySvc,
        external_id_prefix="x",
    )
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    with caplog.at_level("WARNING"):
        result = await adapter.fetch(since=None, cursor={}, max_items=10)

    assert [f["external_id"] for f in result.findings] == ["a", "b"]
    assert result.dropped == 2
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "bad shape" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_runner_adds_idless_findings_to_dropped(monkeypatch):
    from core.federation.registry import FetchResult

    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    fake = _FakeAdapter()

    async def fetch(**_):
        return FetchResult(
            findings=[
                {"finding_id": "f-1", "severity": "high"},
                {"severity": "high"},
            ],
            cursor={"c": 1},
            dropped=2,
        )

    fake.fetch = fetch  # type: ignore[method-assign]
    runner._adapters[fake.name] = fake
    runner._dedup[fake.name] = _FakeDedup()  # type: ignore[assignment]
    calls = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, **kw: calls.append(kw),
    )

    await runner._do_one_tick(fake, {"max_items": 100, "cursor": {}})

    assert queue.qsize() == 1
    assert calls == [{"cursor": {"c": 1}, "dropped": 3}]


@pytest.mark.asyncio
async def test_clean_tick_adds_zero_dropped(monkeypatch):
    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    fake = _FakeAdapter()
    fake.next_findings = [{"finding_id": "f-1", "severity": "high"}]
    runner._adapters[fake.name] = fake
    runner._dedup[fake.name] = _FakeDedup()  # type: ignore[assignment]
    calls = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, **kw: calls.append(kw),
    )

    await runner._do_one_tick(fake, {"max_items": 100, "cursor": {}})

    assert calls[0]["dropped"] == 0


# ---------------------------------------------------------------------------
# Silent-stop paths: adapter import failure, store read errors, disabled integration
# ---------------------------------------------------------------------------


def test_one_adapter_import_failure_leaves_the_others_registered(monkeypatch, caplog):
    modules = fed_registry._BUILTIN_ADAPTER_MODULES
    broken = modules[0]
    imported: List[str] = []

    def fake_import(name):
        if name == broken:
            raise ImportError("boom")
        imported.append(name)

    monkeypatch.setattr(fed_registry, "_BUILTINS_LOADED", False)
    monkeypatch.setattr(fed_registry.importlib, "import_module", fake_import)
    with caplog.at_level("ERROR", logger=fed_registry.logger.name):
        fed_registry._ensure_builtins_loaded()

    assert imported == list(modules[1:])
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert broken in errors[0].getMessage()
    assert errors[0].exc_info is not None


def test_is_active_for_false_when_no_adapter_is_registered(monkeypatch):
    runner = FederationRunner(output_queue=None)
    monkeypatch.setattr(fed_registry, "is_registered", lambda name: False)
    monkeypatch.setattr(
        "core.federation.runner.store.is_globally_enabled", lambda: True
    )
    monkeypatch.setattr(
        "core.federation.runner.store.get_source",
        lambda sid: {"source_id": sid, "enabled": True},
    )
    assert runner.is_active_for("splunk") is False


def test_store_read_errors_log_a_warning_and_read_as_missing(monkeypatch, caplog):
    from core.federation import store

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("core.storage.config_service.get_config_service", boom)
    monkeypatch.setattr("core.storage.connection.get_db_manager", boom)
    with caplog.at_level("WARNING"):
        assert store.get_global_settings() == {"enabled": False}
        assert store.get_source("splunk") is None
        assert store.list_sources() == []
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 3
    # The raising readers let callers tell an outage from "disabled".
    with pytest.raises(RuntimeError):
        store.read_global_settings()
    with pytest.raises(RuntimeError):
        store.read_source("splunk")


@pytest.mark.asyncio
async def test_adapter_loop_reports_a_store_outage_once_and_does_not_poll(
    monkeypatch, caplog
):
    runner = FederationRunner(output_queue=None)
    adapter = _FakeAdapter()
    shutdown = asyncio.Event()
    reads = []

    def failing_read(source_id):
        reads.append(source_id)
        if len(reads) >= 3:
            shutdown.set()
        raise RuntimeError("db down")

    monkeypatch.setattr("core.federation.runner.store.read_source", failing_read)

    async def instant_wait(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("core.federation.runner.asyncio.wait_for", instant_wait)
    with caplog.at_level("WARNING"):
        await runner._adapter_loop(adapter, shutdown)

    assert len(reads) == 3
    assert adapter.fetch_calls == []
    warnings = [r for r in caplog.records if "store read failed" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_unconfigured_integration_is_skipped_not_recorded_as_success(
    monkeypatch, caplog
):
    runner = FederationRunner(output_queue=asyncio.Queue())
    adapter = _FakeAdapter(configured=False)
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda *a, **k: pytest.fail("record_success should not be called"),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda *a, **k: pytest.fail("record_failure should not be called"),
    )
    with caplog.at_level("WARNING"):
        for _ in range(3):
            await runner._do_one_tick(adapter, {"max_items": 10, "cursor": {}})

    assert adapter.fetch_calls == []
    assert runner.stats["polls"] == 0
    assert len([r for r in caplog.records if "not configured" in r.getMessage()]) == 1
