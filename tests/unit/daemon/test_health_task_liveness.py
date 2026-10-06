"""/health reflects whether each component's task is alive (#1588)."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from services.daemon.config import MetricsConfig
from services.daemon.main import SOCDaemon
from services.daemon.metrics import MetricsServer

pytestmark = pytest.mark.unit

NAMES = ("poller", "kafka", "processor", "responder", "scheduler", "orchestrator")


async def _forever():
    await asyncio.Event().wait()


async def _server(**overrides):
    """MetricsServer over stub components, each with a real long-lived task."""
    server = MetricsServer(MetricsConfig())
    server.poller = SimpleNamespace()
    server.kafka_ingestor = SimpleNamespace()
    server.processor = SimpleNamespace()
    server.responder = SimpleNamespace()
    server.scheduler = SimpleNamespace()
    server.orchestrator = SimpleNamespace(enabled=False)
    for name in NAMES:
        server.register_task(
            name, overrides.get(name) or asyncio.create_task(_forever())
        )
    return server


async def _health(server):
    resp = await server._handle_health(None)
    return resp.status, json.loads(resp.body)


async def _cleanup(server):
    for task in server._tasks.values():
        task.cancel()
    await asyncio.gather(*server._tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_all_alive_with_orchestrator_disabled_is_healthy():
    server = await _server()
    status, body = await _health(server)
    await _cleanup(server)

    assert status == 200
    assert body["status"] == "healthy"
    assert body["components"]["orchestrator"] == "disabled"
    assert body["components"]["kafka"] == "running"


@pytest.mark.asyncio
async def test_raised_task_is_failed_and_unhealthy():
    async def boom():
        raise OSError("port in use")

    task = asyncio.create_task(boom())
    await asyncio.gather(task, return_exceptions=True)
    server = await _server(poller=task)
    status, body = await _health(server)
    await _cleanup(server)

    assert status == 503
    assert body["status"] == "unhealthy"
    assert body["components"]["poller"] == "failed: OSError"


@pytest.mark.asyncio
async def test_returned_or_dead_disabled_orchestrator_task_is_unhealthy():
    async def returns():
        return None

    done = asyncio.create_task(returns())
    await done
    server = await _server(orchestrator=done)
    status, body = await _health(server)
    await _cleanup(server)

    assert status == 503
    assert body["components"]["orchestrator"] == "stopped"


@pytest.mark.asyncio
async def test_missing_task_is_not_initialized():
    server = await _server()
    del server._tasks["scheduler"]
    status, body = await _health(server)
    await _cleanup(server)

    assert status == 503
    assert body["components"]["scheduler"] == "not_initialized"


def _daemon():
    daemon = SOCDaemon.__new__(SOCDaemon)
    daemon._shutdown_event = asyncio.Event()
    return daemon


@pytest.mark.asyncio
async def test_done_callback_logs_error_on_unexpected_raise(caplog):
    daemon = _daemon()

    async def boom():
        raise OSError("port in use")

    task = asyncio.create_task(boom())
    await asyncio.gather(task, return_exceptions=True)
    with caplog.at_level(logging.ERROR):
        daemon._on_task_done("poller", task)

    [record] = caplog.records
    assert "poller" in record.getMessage() and "OSError" in record.getMessage()
    assert record.exc_info[0] is OSError


@pytest.mark.asyncio
async def test_done_callback_logs_unexpected_return_but_not_shutdown(caplog):
    daemon = _daemon()

    async def returns():
        return None

    task = asyncio.create_task(returns())
    await task
    with caplog.at_level(logging.ERROR):
        daemon._on_task_done("scheduler", task)
        assert len(caplog.records) == 1
        caplog.clear()
        daemon._shutdown_event.set()
        daemon._on_task_done("scheduler", task)
    assert not caplog.records


@pytest.mark.asyncio
async def test_done_callback_is_quiet_on_cancellation(caplog):
    daemon = _daemon()
    task = asyncio.create_task(_forever())
    task.add_done_callback(lambda t: daemon._on_task_done("poller", t))
    with caplog.at_level(logging.ERROR):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert not caplog.records
