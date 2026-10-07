"""SOC Daemon - Main entry point and orchestration."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# Add the repo root to sys.path (this file is services/daemon/main.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import validate_settings_or_exit

if TYPE_CHECKING:
    from services.daemon.config import DaemonConfig

logger = logging.getLogger(__name__)


class SOCDaemon:
    """Main daemon orchestrator for autonomous SOC operations."""

    def __init__(self, config: Optional[DaemonConfig] = None):
        if config is None:
            from services.daemon.config import DaemonConfig

            config = DaemonConfig.from_env()
        self.config = config
        self.config.setup_logging()

        # Observe mode (#915): log declared-vs-effective intent, enforce nothing.
        try:
            from services.daemon.intent import report_intent

            report_intent(self.config)
        except Exception as _intent_err:
            logger.warning("Intent report failed (non-fatal): %s", _intent_err)

        # Initialize OTEL telemetry after logging is set up
        try:
            from core.telemetry import init_telemetry

            init_telemetry("vigil-daemon")
        except Exception as _tel_err:
            logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

        self._running = False
        self._shutdown_event = asyncio.Event()

        # Components (lazy loaded)
        self._poller = None
        self._kafka_ingestor = None
        self._processor = None
        self._responder = None
        self._scheduler = None
        self._orchestrator = None
        self._metrics_server = None
        self._mcp_client = None

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
        from core.response.approval_service import ApprovalService
        from core.response.autonomous_response_service import AutonomousResponseService
        from core.storage.connection import get_db_manager
        from services.daemon.kafka_ingestor import KafkaIngestor
        from services.daemon.metrics import MetricsServer
        from services.daemon.orchestrator import Orchestrator
        from services.daemon.poller import DataPoller
        from services.daemon.processor import FindingProcessor
        from services.daemon.responder import AutonomousResponder
        from services.daemon.scheduler import TaskScheduler

        # Resolve the DB credentials now so a missing password stops startup,
        # rather than surfacing on the first query inside a component task.
        get_db_manager()

        self._poller = DataPoller(self.config.polling)
        self._kafka_ingestor = KafkaIngestor(self.config.kafka)
        self._processor = FindingProcessor(
            self.config.processing, response_config=self.config.response
        )
        # The daemon owns its own copies: it is a separate process from the API, so
        # nothing on the API's app.state is reachable from here.
        self._mcp_client = build_mcp_client()
        set_process_mcp_client(self._mcp_client)
        approvals = ApprovalService(config=self.config.response)

        self._responder = AutonomousResponder(
            self.config.response,
            self.config.escalation,
            response_service=AutonomousResponseService(
                approvals=approvals, config=self.config.response
            ),
            approvals=approvals,
        )
        self._scheduler = TaskScheduler(self.config.scheduler)
        self._orchestrator = Orchestrator(
            self.config.orchestrator,
            approvals=approvals,
        )

        if self.config.metrics.enabled:
            self._metrics_server = MetricsServer(self.config.metrics)

        # Connect components via queues
        self._poller.set_output_queue(self._processor.input_queue)
        self._kafka_ingestor.set_output_queue(self._processor.input_queue)
        self._processor.set_response_queue(self._responder.input_queue)
        self._scheduler.set_processor_queue(self._processor.input_queue)

        # Wire up metrics server with component references
        if self._metrics_server:
            self._metrics_server.poller = self._poller
            self._metrics_server.kafka_ingestor = self._kafka_ingestor
            self._metrics_server.processor = self._processor
            self._metrics_server.responder = self._responder
            self._metrics_server.scheduler = self._scheduler
            self._metrics_server.orchestrator = self._orchestrator

        logger.info("All components initialized")

    async def run(self):
        """Run the daemon."""
        logger.info("Starting SOC Daemon...")
        self._running = True

        try:
            self._setup_signal_handlers()
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            logger.warning("Signal handlers not supported on this platform")

        await self._init_components()

        # Start all component tasks
        tasks = []

        def start(name: str, component, label: str):
            task = asyncio.create_task(component.run(self._shutdown_event))
            task.add_done_callback(lambda t: self._on_task_done(name, t))
            if self._metrics_server and component is not self._metrics_server:
                self._metrics_server.register_task(name, task)
            tasks.append(task)
            logger.info("%s started", label)

        if self._poller:
            start("poller", self._poller, "Data poller")

        if self._kafka_ingestor:
            start("kafka", self._kafka_ingestor, "Kafka ingestor")

        if self._processor:
            start("processor", self._processor, "Finding processor")

        if self._responder:
            start("responder", self._responder, "Autonomous responder")

        if self._scheduler:
            start("scheduler", self._scheduler, "Task scheduler")

        if self._orchestrator:
            start("orchestrator", self._orchestrator, "Autonomous orchestrator")
            if not self.config.orchestrator.enabled:
                logger.info("Autonomous orchestrator is disabled")

        if self._metrics_server:
            start("metrics", self._metrics_server, "Metrics server")
            logger.info(
                "Health :%d, prometheus :%d",
                self._metrics_server.health_port,
                self._metrics_server.metrics_port,
            )

        logger.info("SOC Daemon fully operational")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        logger.info("Shutting down daemon components...")

        # Cancel all tasks
        for task in tasks:
            task.cancel()

        # Wait for tasks to complete
        await asyncio.gather(*tasks, return_exceptions=True)

        self._running = False

        # Flush and shut down OTEL providers
        try:
            from core.telemetry import shutdown_telemetry

            shutdown_telemetry()
        except Exception as e:
            logger.warning("Telemetry shutdown error (non-fatal): %s", e)

        logger.info("SOC Daemon shutdown complete")

    async def stop(self):
        """Stop the daemon gracefully."""
        self._shutdown_event.set()


def main():
    """Entry point for the daemon."""
    validate_settings_or_exit()
    from services.daemon.config import DaemonConfig

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
