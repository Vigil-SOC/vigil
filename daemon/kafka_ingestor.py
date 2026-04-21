"""Daemon-side wrapper that runs the Kafka consumer.

Mirrors the shape of ``daemon/poller.py``'s ``DataPoller`` so
``SOCDaemon`` can plug it in the same way: instantiate, hand it the
shared output queue, start its ``run()`` as an asyncio task.

The Kafka broker config is re-read from the SystemConfig ``kafka.settings``
row on each startup attempt, so toggling ``enabled`` in the Settings UI
starts or stops the consumer on the next sync tick.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from daemon.config import KafkaConfig
from daemon.dedup import RedisDedupSet
from services.kafka_consumer_service import KafkaConsumerService

logger = logging.getLogger(__name__)


class KafkaIngestor:
    """Starts/stops the Kafka consumer based on the enabled flag."""

    SYNC_INTERVAL_SECONDS = 5

    def __init__(self, config: KafkaConfig):
        self.config = config
        self._output_queue: Optional[asyncio.Queue] = None
        self._dedup = RedisDedupSet("kafka")
        self._service: Optional[KafkaConsumerService] = None
        self._task: Optional[asyncio.Task] = None
        self._consumer_shutdown: Optional[asyncio.Event] = None

    @property
    def stats(self) -> Dict[str, Any]:
        if self._service is not None:
            return self._service.stats
        return {
            "connected": False,
            "messages_consumed": 0,
            "messages_enqueued": 0,
            "duplicates_skipped": 0,
            "decode_errors": 0,
            "missing_id_errors": 0,
            "last_message_at": None,
            "last_error": None,
            "last_error_at": None,
            "topics": list(self.config.topics),
            "consumer_group": self.config.consumer_group,
        }

    def set_output_queue(self, queue: asyncio.Queue) -> None:
        self._output_queue = queue

    def _sync_config_from_db(self) -> None:
        """Re-read ``kafka.settings`` from SystemConfig (non-secret fields only)."""
        try:
            from database.config_service import get_config_service

            db_cfg = get_config_service().get_system_config("kafka.settings")
            if not db_cfg or not isinstance(db_cfg, dict):
                return
            if "enabled" in db_cfg:
                self.config.enabled = bool(db_cfg["enabled"])
            if "bootstrap_servers" in db_cfg:
                self.config.bootstrap_servers = str(db_cfg["bootstrap_servers"])
            if "consumer_group" in db_cfg:
                self.config.consumer_group = str(db_cfg["consumer_group"])
            if "topics" in db_cfg and isinstance(db_cfg["topics"], list):
                self.config.topics = [str(t) for t in db_cfg["topics"]]
            if "auto_offset_reset" in db_cfg:
                self.config.auto_offset_reset = str(db_cfg["auto_offset_reset"])
            if "security_protocol" in db_cfg:
                self.config.security_protocol = str(db_cfg["security_protocol"])
        except Exception as e:
            logger.debug("Kafka config sync from DB failed (non-fatal): %s", e)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Start/stop the consumer based on the enabled flag; exit on shutdown."""
        if self._output_queue is None:
            logger.error("KafkaIngestor: output queue not set, exiting")
            return

        logger.info("Kafka ingestor started (waiting for enabled flag)")

        while not shutdown_event.is_set():
            self._sync_config_from_db()

            should_run = self.config.enabled and bool(self.config.topics)
            is_running = self._task is not None and not self._task.done()

            if should_run and not is_running:
                logger.info("Kafka ingestor: starting consumer task")
                self._consumer_shutdown = asyncio.Event()
                self._service = KafkaConsumerService(
                    self.config, self._output_queue, self._dedup
                )
                self._task = asyncio.create_task(
                    self._service.run(self._consumer_shutdown)
                )
            elif not should_run and is_running:
                reason = (
                    "disabled" if not self.config.enabled else "no topics configured"
                )
                logger.info("Kafka ingestor: stopping consumer (%s)", reason)
                await self._stop_consumer()

            # Wait until next sync tick or shutdown
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self.SYNC_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

        # Global shutdown
        await self._stop_consumer()
        logger.info("Kafka ingestor stopped")

    async def _stop_consumer(self) -> None:
        if self._consumer_shutdown is not None:
            self._consumer_shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Kafka consumer did not stop within 30s, cancelling")
                self._task.cancel()
                try:
                    await self._task
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Kafka consumer stop error: %s", e)
        self._task = None
        self._service = None
        self._consumer_shutdown = None
