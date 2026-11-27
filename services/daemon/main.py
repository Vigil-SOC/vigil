"""SOC Daemon - Main entry point and orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Add the repo root to sys.path (this file is services/daemon/main.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import validate_settings_or_exit

if TYPE_CHECKING:
    from core.daemon_config import DaemonConfig

logger = logging.getLogger(__name__)

# Under the daemon StatefulSet's terminationGracePeriodSeconds (60), leaving
# room for the telemetry flush after the components stop.
# Compose stop_grace_period is at least this, so a restart drains before SIGKILL.
SHUTDOWN_GRACE_SECONDS = 45


class SOCDaemon:
    """Main daemon orchestrator for autonomous SOC operations."""

    def __init__(self, config: DaemonConfig):
        self.config = config
        self.config.setup_logging()

        # Log declared-vs-effective intent (#915). Enforcement, when a
        # directory is declared, is the applier task started in run().
        try:
            from core.intent_report import report_intent

            report_intent(self.config)
        except Exception as _intent_err:
            logger.warning("Intent report failed (non-fatal): %s", _intent_err)

        # Initialize OTEL telemetry after logging is set up
        try:
            from core.telemetry import init_telemetry

            init_telemetry("vigil-daemon")
        except Exception as _tel_err:
            logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

        self._shutdown_event = asyncio.Event()

        # Components (lazy loaded)
        self._poller = None
        self._kafka_ingestor = None
        self._processor = None
        self._scheduler = None
        self._orchestrator = None
        self._metrics_server = None

        logger.info("SOC Daemon initialized")

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

    def _handle_shutdown(self):
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        self._shutdown_event.set()

    def _on_task_done(self, name: str, task: asyncio.Task) -> None:
        """Log the moment a component task dies outside of shutdown."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Component task '%s' failed: %s", name, type(exc).__name__, exc_info=exc
            )
        elif not self._shutdown_event.is_set():
            logger.error("Component task '%s' exited unexpectedly", name)

    async def _init_components(self):
        """Initialize all daemon components."""
        logger.info("Initializing daemon components...")

        # Import here to avoid circular imports
        from core.integrations.mcp.client import (
            build_mcp_client,
            set_process_mcp_client,
        )
        from core.storage.connection import get_db_manager
        from services.daemon.kafka_ingestor import KafkaIngestor
        from services.daemon.metrics import MetricsServer
        from services.daemon.orchestrator import Orchestrator
        from services.daemon.poller import DataPoller
        from services.daemon.processor import FindingProcessor
        from services.daemon.scheduler import TaskScheduler

        # Resolve the DB credentials now so a missing password stops startup,
        # rather than surfacing on the first query inside a component task.
        get_db_manager()

        self._poller = DataPoller(self.config.polling)
        self._kafka_ingestor = KafkaIngestor(self.config.kafka)
        self._processor = FindingProcessor(self.config.processing)
        # The daemon owns its own copies: it is a separate process from the API, so
        # nothing on the API's app.state is reachable from here.
        set_process_mcp_client(build_mcp_client())

        self._scheduler = TaskScheduler(self.config.scheduler)
        self._orchestrator = Orchestrator(self.config.orchestrator)

        self._metrics_server = MetricsServer(self.config.metrics)

        # Connect components via queues
        self._poller.set_output_queue(self._processor.input_queue)
        self._kafka_ingestor.set_output_queue(self._processor.input_queue)
        self._scheduler.set_processor_queue(self._processor.input_queue)

        # Wire up metrics server with component references
        self._metrics_server.poller = self._poller
        self._metrics_server.kafka_ingestor = self._kafka_ingestor
        self._metrics_server.processor = self._processor
        self._metrics_server.scheduler = self._scheduler
        self._metrics_server.orchestrator = self._orchestrator

        # Sub-loop death shows on /status only; /health is unchanged.
        self._poller.set_loop_watcher(self._metrics_server.loop_watcher("federation"))
        self._processor.watch_loop = self._metrics_server.loop_watcher("processor")
        self._orchestrator.watch_loop = self._metrics_server.loop_watcher(
            "orchestrator"
        )

        tick = self._metrics_server.pass_ticker
        self._poller.set_pass_ticker(tick("poller"))
        self._processor.note_pass = tick("processor")
        self._scheduler.note_pass = tick("scheduler")
        self._orchestrator.note_pass = tick("orchestrator")

        logger.info("All components initialized")

    async def run(self):
        """Run the daemon."""
        logger.info("Starting SOC Daemon...")

        try:
            self._setup_signal_handlers()
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            logger.warning("Signal handlers not supported on this platform")

        await self._init_components()

        # Start all component tasks
        tasks = []

        def start(name: str, component, label: str):
            task = asyncio.create_task(component.run(self._shutdown_event), name=name)
            task.add_done_callback(lambda t: self._on_task_done(name, t))
            if component is not self._metrics_server:
                self._metrics_server.register_task(name, task)
            tasks.append(task)
            logger.info("%s started", label)

        if self._poller:
            start("poller", self._poller, "Data poller")

        if self._kafka_ingestor:
            start("kafka", self._kafka_ingestor, "Kafka ingestor")

        if self._processor:
            start("processor", self._processor, "Finding processor")

        if self._scheduler:
            start("scheduler", self._scheduler, "Task scheduler")

        if self._orchestrator:
            start("orchestrator", self._orchestrator, "Autonomous orchestrator")
            if not self.config.orchestrator.enabled:
                logger.info("Autonomous orchestrator is disabled")

        start("metrics", self._metrics_server, "Metrics server")

        # The daemon's own MCP client re-merges accepted declared sources too.
        from core.integrations.mcp.client import process_mcp_client
        from core.integrations.mcp.reload import run_declared_sources

        if process_mcp_client() is not None:
            task = asyncio.create_task(
                run_declared_sources(process_mcp_client(), None, self._shutdown_event),
                name="sources",
            )
            task.add_done_callback(lambda t: self._on_task_done("sources", t))
            tasks.append(task)

        # A declared directory (VIGIL_INTENT_DIR): this daemon is its only applier.
        from services.daemon.intent_apply import (
            clear_site,
            clear_sources,
            intent_dir,
            run_applier,
        )

        directory = intent_dir()
        if directory is None:
            try:  # unset: the declared site goes; intent.applied stays
                await asyncio.to_thread(clear_site)
            except Exception:  # noqa: BLE001 - never stops the daemon
                logger.exception("site: clearing the declared site failed")
            try:  # and declared sources, in their own try
                await asyncio.to_thread(clear_sources)
            except Exception:  # noqa: BLE001 - never stops the daemon
                logger.exception("sources: clearing declared sources failed")
        else:
            task = asyncio.create_task(
                run_applier(directory, self._shutdown_event), name="intent"
            )
            task.add_done_callback(lambda t: self._on_task_done("intent", t))
            tasks.append(task)

        logger.info("SOC Daemon fully operational")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        logger.info("Shutting down daemon components...")

        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            logger.warning(
                "Component task '%s' did not stop within %ds, cancelling",
                task.get_name(),
                SHUTDOWN_GRACE_SECONDS,
            )
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Flush and shut down OTEL providers
        try:
            from core.telemetry import shutdown_telemetry

            shutdown_telemetry()
        except Exception as e:
            logger.warning("Telemetry shutdown error (non-fatal): %s", e)

        logger.info("SOC Daemon shutdown complete")


def main():
    """Entry point for the daemon."""
    validate_settings_or_exit()
    from core.daemon_config import DaemonConfig

    config = DaemonConfig.from_env()
    daemon = SOCDaemon(config)

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by user")
    except Exception as e:
        logger.error(f"Daemon error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
