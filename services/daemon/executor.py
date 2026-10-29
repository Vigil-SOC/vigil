"""Executes approved response actions for the SOC daemon."""

import asyncio
import logging

from core.response.autonomous_response_service import AutonomousResponseService

logger = logging.getLogger(__name__)


class ApprovedActionExecutor:
    """Carries out actions a person approved, from any source, every 30 seconds.

    ``execution_held()`` (Stop) inside ``execute_approved_actions`` is the only hold.
    """

    def __init__(self, response_service: AutonomousResponseService):
        self._response_service = response_service
        self.stats = {"executed": 0, "failed": 0, "errors": 0}

    async def run(self, shutdown_event: asyncio.Event):
        """Run the executor loop."""
        logger.info("Approved-action executor starting...")
        while not shutdown_event.is_set():
            try:
                # Sync DB and HTTP calls: keep them off the event loop.
                results = await asyncio.to_thread(
                    self._response_service.execute_approved_actions
                )
                for result in results:
                    if result.get("result", {}).get("success"):
                        self.stats["executed"] += 1
                        logger.info(
                            "Executed approved action: %s", result.get("action_id")
                        )
                    else:
                        self.stats["failed"] += 1
                        logger.warning("Action execution failed: %s", result)
            except Exception as e:
                self.stats["errors"] += 1
                logger.error("Action executor error: %s", e)

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass
        logger.info("Approved-action executor stopped")
