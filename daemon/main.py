"""SOC Daemon - Main entry point and orchestration."""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.config import DaemonConfig

logger = logging.getLogger(__name__)


class SOCDaemon:
    """Main daemon orchestrator for autonomous SOC operations."""
    
    def __init__(self, config: Optional[DaemonConfig] = None):
        self.config = config or DaemonConfig.from_env()
        self.config.setup_logging()

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
        self._processor = None
        self._responder = None
        self._scheduler = None
        self._orchestrator = None
        self._llm_worker_manager = None
        self._metrics_server = None
        
        logger.info("SOC Daemon initialized")
    
    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        loop = asyncio.get_event_loop()
        
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)
    
    def _handle_shutdown(self):
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        self._shutdown_event.set()
    
    async def _init_components(self):
        """Initialize all daemon components."""
        logger.info("Initializing daemon components...")
        
        # Import here to avoid circular imports
        from daemon.poller import DataPoller
        from daemon.processor import FindingProcessor
        from daemon.responder import AutonomousResponder
        from daemon.scheduler import TaskScheduler
        from daemon.metrics import MetricsServer
        from daemon.orchestrator import Orchestrator
        from daemon.llm_worker_manager import LLMWorkerManager

        self._poller = DataPoller(self.config.polling)
        self._processor = FindingProcessor(self.config.processing)
        self._responder = AutonomousResponder(self.config.response, self.config.escalation)
        self._scheduler = TaskScheduler(self.config.scheduler)
        self._orchestrator = Orchestrator(self.config.orchestrator)
        self._llm_worker_manager = LLMWorkerManager()

        if self.config.metrics.enabled:
            self._metrics_server = MetricsServer(self.config.metrics)
        
        # Connect components via queues
        self._poller.set_output_queue(self._processor.input_queue)
        self._processor.set_response_queue(self._responder.input_queue)
        self._processor.set_investigation_queue(self._orchestrator.investigation_queue)
        
        # Wire up metrics server with component references
        if self._metrics_server:
            self._metrics_server.poller = self._poller
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
        
        if self._poller:
            tasks.append(asyncio.create_task(self._poller.run(self._shutdown_event)))
            logger.info("Data poller started")
        
        if self._processor:
            tasks.append(asyncio.create_task(self._processor.run(self._shutdown_event)))
            logger.info("Finding processor started")
        
        if self._responder:
            tasks.append(asyncio.create_task(self._responder.run(self._shutdown_event)))
            logger.info("Autonomous responder started")
        
        if self._scheduler:
            tasks.append(asyncio.create_task(self._scheduler.run(self._shutdown_event)))
            logger.info("Task scheduler started")
        
        if self._orchestrator:
            tasks.append(asyncio.create_task(self._orchestrator.run(self._shutdown_event)))
            if self.config.orchestrator.enabled:
                logger.info("Autonomous orchestrator started")
            else:
                logger.info("Autonomous orchestrator loaded (disabled)")

        if self._llm_worker_manager:
            tasks.append(asyncio.create_task(self._llm_worker_manager.run(self._shutdown_event)))
            logger.info("LLM Worker Manager started (controls worker subprocess via DB toggle)")

        if self._metrics_server:
            tasks.append(asyncio.create_task(self._metrics_server.run(self._shutdown_event)))
            logger.info(f"Metrics server started on port {self.config.metrics.port}")
        
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
