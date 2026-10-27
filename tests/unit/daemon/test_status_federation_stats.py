"""Federation process counters on GET /status."""

import json
from types import SimpleNamespace

import pytest

from core.federation.contract import FetchResult
from core.federation.runner import FederationRunner
from services.daemon.config import MetricsConfig
from services.daemon.metrics import MetricsServer
from tests.unit._acking_queue import AckingQueue

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr("services.daemon.metrics.configured_redis_url", lambda: None)


class _Adapter:
    name = "fake"

    def __init__(self, result=None, error=None):
        self._result = result or FetchResult()
        self._error = error

    def is_configured(self):
        return True

    async def fetch(self, *, cursor, max_items):
        if self._error is not None:
            raise self._error
        return self._result


class _Dedup:
    async def are_processed(self, keys):
        return set()

    async def mark_many(self, keys):
        return None


def _silence_store(monkeypatch):
    monkeypatch.setattr(
        "core.federation.runner.store.record_success", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure", lambda *args, **kwargs: None
    )


async def _status(runner):
    server = MetricsServer(MetricsConfig())
    server.poller = SimpleNamespace(stats={}, _federation=runner)
    return json.loads((await server._handle_status(None)).text)["federation"]


@pytest.mark.asyncio
async def test_tick_that_drops_two_records(monkeypatch):
    _silence_store(monkeypatch)
    runner = FederationRunner(output_queue=AckingQueue())
    await runner._do_one_tick(
        _Adapter(FetchResult(findings=[], dropped=2, cursor={})),
        {"max_items": 10, "cursor": {}},
    )
    assert await _status(runner) == {
        "polls": 1,
        "findings": 0,
        "errors": 0,
        "dropped": 2,
    }


@pytest.mark.asyncio
async def test_tick_with_no_drops_reports_zero(monkeypatch):
    _silence_store(monkeypatch)
    runner = FederationRunner(output_queue=AckingQueue())
    runner._dedup["fake"] = _Dedup()
    await runner._do_one_tick(
        _Adapter(
            FetchResult(
                findings=[
                    {
                        "finding_id": "f-1",
                        "external_id": "ext-1",
                        "severity": "high",
                    }
                ],
                dropped=0,
                cursor={"n": 1},
            )
        ),
        {"max_items": 10, "cursor": {}},
    )
    assert await _status(runner) == {
        "polls": 1,
        "findings": 1,
        "errors": 0,
        "dropped": 0,
    }


@pytest.mark.asyncio
async def test_both_record_failure_paths_count_as_errors(monkeypatch):
    failures = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append(source_id),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_success", lambda *args, **kwargs: None
    )

    raised = FederationRunner(output_queue=AckingQueue())
    await raised._do_one_tick(
        _Adapter(error=RuntimeError("boom")),
        {"max_items": 10, "cursor": {}},
    )
    assert await _status(raised) == {
        "polls": 1,
        "findings": 0,
        "errors": 1,
        "dropped": 0,
    }

    unstored = FederationRunner(output_queue=AckingQueue(stored=False))
    unstored._dedup["fake"] = _Dedup()
    await unstored._do_one_tick(
        _Adapter(
            FetchResult(
                findings=[{"finding_id": "f-2", "external_id": "ext-2"}],
                cursor={},
            )
        ),
        {"max_items": 10, "cursor": {}},
    )
    counted = await _status(unstored)
    assert counted["errors"] == 1
    assert counted["polls"] == 1
    assert counted["findings"] == 0
    assert counted["dropped"] == 0
    assert failures == ["fake", "fake"]


@pytest.mark.asyncio
async def test_unconfigured_adapter_is_not_a_poll(monkeypatch):
    _silence_store(monkeypatch)
    runner = FederationRunner(output_queue=AckingQueue())
    adapter = _Adapter()
    adapter.is_configured = lambda: False
    await runner._do_one_tick(adapter, {"max_items": 10, "cursor": {}})
    assert await _status(runner) == {
        "polls": 0,
        "findings": 0,
        "errors": 0,
        "dropped": 0,
    }
