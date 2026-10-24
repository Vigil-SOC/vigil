"""SIGTERM lets each component run its post-shutdown code (#3134)."""

import asyncio
from types import SimpleNamespace

import pytest

from services.daemon import main as daemon_main
from services.daemon.main import SOCDaemon


def _daemon(**components):
    daemon = SOCDaemon.__new__(SOCDaemon)
    daemon.config = SimpleNamespace(orchestrator=SimpleNamespace(enabled=False))
    daemon._running = False
    daemon._shutdown_event = asyncio.Event()
    for name in (
        "poller",
        "kafka_ingestor",
        "processor",
        "responder",
        "scheduler",
        "orchestrator",
        "metrics_server",
    ):
        setattr(daemon, f"_{name}", components.get(name))
    daemon._setup_signal_handlers = lambda: None

    async def _no_init():
        return None

    daemon._init_components = _no_init
    return daemon


async def _run_then_sigterm(daemon):
    run = asyncio.create_task(daemon.run())
    for _ in range(20):
        await asyncio.sleep(0)
    daemon._handle_shutdown()
    await asyncio.wait_for(run, timeout=10)


@pytest.mark.asyncio
async def test_component_cleanup_after_shutdown_event_runs():
    cleaned = []

    class Stub:
        async def run(self, shutdown_event):
            await shutdown_event.wait()
            await asyncio.sleep(0.05)
            cleaned.append(True)

    await _run_then_sigterm(_daemon(processor=Stub()))
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_component_past_grace_is_cancelled(monkeypatch):
    monkeypatch.setattr(daemon_main, "SHUTDOWN_GRACE_SECONDS", 0.1)
    cancelled = []
    cleaned = []

    class Stuck:
        async def run(self, shutdown_event):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    class Prompt:
        async def run(self, shutdown_event):
            await shutdown_event.wait()
            cleaned.append(True)

    await _run_then_sigterm(_daemon(processor=Stuck(), responder=Prompt()))
    assert cancelled == [True]
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_orchestrator_abandons_in_flight_on_sigterm(monkeypatch):
    from services.daemon.config import OrchestratorConfig
    from services.daemon.orchestrator import Orchestrator

    orch = Orchestrator(OrchestratorConfig(enabled=False))
    monkeypatch.setattr(orch, "_sync_enabled_from_db", lambda: None)
    calls = []
    monkeypatch.setattr(orch, "_abandon_in_flight", lambda: calls.append(1))

    await _run_then_sigterm(_daemon(orchestrator=orch))
    assert calls == [1]


@pytest.mark.asyncio
async def test_kafka_ingestor_stops_consumer_on_sigterm(monkeypatch):
    from services.daemon.config import KafkaConfig
    from services.daemon.kafka_ingestor import KafkaIngestor

    ing = KafkaIngestor(KafkaConfig())
    ing._output_queue = asyncio.Queue()
    monkeypatch.setattr(ing, "_sync_config_from_db", lambda: None)
    calls = []

    async def _stop():
        calls.append(1)

    monkeypatch.setattr(ing, "_stop_consumer", _stop)
    await _run_then_sigterm(_daemon(kafka_ingestor=ing))
    assert calls == [1]


@pytest.mark.asyncio
async def test_scheduler_cancels_rates_refresher_on_sigterm(monkeypatch):
    from services.daemon import scheduler as scheduler_mod
    from services.daemon.config import SchedulerConfig

    refresher_cancelled = []

    async def _noop():
        return None

    async def _refresher():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresher_cancelled.append(True)
            raise

    monkeypatch.setattr(scheduler_mod, "refresh_gateway_rates", _noop)
    monkeypatch.setattr(scheduler_mod, "run_gateway_rates_refresher", _refresher)
    sched = scheduler_mod.TaskScheduler(SchedulerConfig())
    monkeypatch.setattr(sched, "_init_services", lambda: None)
    sched._tasks = []

    await _run_then_sigterm(_daemon(scheduler=sched))
    await asyncio.sleep(0)
    assert refresher_cancelled == [True]
