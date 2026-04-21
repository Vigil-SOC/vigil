"""Kafka consumer service for streaming finding ingestion.

Subscribes to one or more Kafka topics and forwards each JSON message
to the daemon's finding queue (``daemon/processor.py`` consumes from
the same queue as ``daemon/poller.py``'s output). Per-message flow:

    Kafka record -> JSON decode -> dedup check (Redis) ->
    enqueue for processing -> mark processed -> commit offset

MVP scope: JSON only. No Avro, no Schema Registry, no DLQ.
Malformed messages are logged and skipped; the consumer keeps going.

Each message must be a finding dict matching what
``services/ingestion_service.IngestionService.ingest_finding`` accepts:
at minimum a ``finding_id`` field (string). ``data_source`` will be
auto-set to ``"kafka:<topic>"`` if the producer didn't set one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from daemon.config import KafkaConfig
from daemon.dedup import RedisDedupSet

logger = logging.getLogger(__name__)


class KafkaConsumerService:
    """Async Kafka consumer that feeds findings into the daemon pipeline."""

    def __init__(
        self,
        config: KafkaConfig,
        output_queue: asyncio.Queue,
        dedup: Optional[RedisDedupSet] = None,
    ):
        self.config = config
        self._output_queue = output_queue
        self._dedup = dedup or RedisDedupSet("kafka")
        self._consumer = None
        self._running = False

        self.stats: Dict[str, Any] = {
            "connected": False,
            "messages_consumed": 0,
            "messages_enqueued": 0,
            "duplicates_skipped": 0,
            "decode_errors": 0,
            "missing_id_errors": 0,
            "last_message_at": None,
            "last_error": None,
            "last_error_at": None,
            "topics": list(config.topics),
            "consumer_group": config.consumer_group,
        }

    async def _build_consumer(self):
        """Instantiate an ``AIOKafkaConsumer`` from our config."""
        from aiokafka import AIOKafkaConsumer  # lazy import

        kwargs: Dict[str, Any] = dict(
            bootstrap_servers=self.config.bootstrap_servers,
            group_id=self.config.consumer_group,
            auto_offset_reset=self.config.auto_offset_reset,
            enable_auto_commit=False,
            max_poll_records=self.config.max_poll_records,
            session_timeout_ms=self.config.session_timeout_ms,
            security_protocol=self.config.security_protocol,
        )
        if self.config.sasl_mechanism:
            kwargs["sasl_mechanism"] = self.config.sasl_mechanism
            kwargs["sasl_plain_username"] = self.config.sasl_username
            kwargs["sasl_plain_password"] = self.config.sasl_password
        if self.config.ssl_ca_location:
            import ssl

            ssl_ctx = ssl.create_default_context(cafile=self.config.ssl_ca_location)
            kwargs["ssl_context"] = ssl_ctx

        if not self.config.topics:
            raise ValueError("KafkaConsumerService: no topics configured")

        return AIOKafkaConsumer(*self.config.topics, **kwargs)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main consumer loop. Exits when ``shutdown_event`` is set."""
        if not self.config.topics:
            logger.warning("Kafka consumer: no topics configured, refusing to start")
            return

        logger.info(
            "Kafka consumer starting (group=%s topics=%s servers=%s)",
            self.config.consumer_group,
            ",".join(self.config.topics),
            self.config.bootstrap_servers,
        )

        try:
            self._consumer = await self._build_consumer()
            await self._consumer.start()
            self.stats["connected"] = True
            self._running = True
            logger.info("Kafka consumer connected")
        except Exception as e:
            self.stats["connected"] = False
            self._record_error(f"startup failed: {e}")
            logger.error("Kafka consumer failed to start: %s", e)
            return

        try:
            while not shutdown_event.is_set():
                try:
                    # getmany so we can check shutdown frequently
                    batches = await self._consumer.getmany(timeout_ms=1000)
                    for tp, msgs in batches.items():
                        for msg in msgs:
                            await self._handle_message(tp.topic, msg)
                        # Commit offsets for this partition after processing
                        if msgs:
                            await self._consumer.commit()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._record_error(f"poll error: {e}")
                    logger.error("Kafka consumer poll error: %s", e)
                    # Brief backoff before retry
                    await asyncio.sleep(2)
        finally:
            self._running = False
            self.stats["connected"] = False
            try:
                if self._consumer is not None:
                    await self._consumer.stop()
                    logger.info("Kafka consumer stopped cleanly")
            except Exception as e:
                logger.warning("Kafka consumer stop error: %s", e)
            try:
                await self._dedup.close()
            except Exception:
                pass

    async def _handle_message(self, topic: str, msg) -> None:
        """Decode, dedupe, and enqueue a single Kafka message."""
        self.stats["messages_consumed"] += 1
        self.stats["last_message_at"] = datetime.utcnow().isoformat()

        try:
            raw = msg.value
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            finding = json.loads(raw)
        except Exception as e:
            self.stats["decode_errors"] += 1
            self._record_error(f"decode error on topic {topic}: {e}")
            logger.warning("Kafka: skipping malformed message on %s: %s", topic, e)
            return

        if not isinstance(finding, dict):
            self.stats["decode_errors"] += 1
            logger.warning(
                "Kafka: skipping non-object message on %s (type=%s)",
                topic,
                type(finding).__name__,
            )
            return

        finding_id = finding.get("finding_id")
        if not finding_id:
            self.stats["missing_id_errors"] += 1
            logger.warning("Kafka: skipping message on %s with no finding_id", topic)
            return

        if await self._dedup.is_processed(finding_id):
            self.stats["duplicates_skipped"] += 1
            logger.debug("Kafka: duplicate finding_id=%s skipped", finding_id)
            return

        finding.setdefault("data_source", f"kafka:{topic}")

        await self._output_queue.put(
            {
                "type": "finding",
                "source": f"kafka:{topic}",
                "data": finding,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        await self._dedup.mark_processed(finding_id)
        self.stats["messages_enqueued"] += 1

    def _record_error(self, msg: str) -> None:
        self.stats["last_error"] = msg
        self.stats["last_error_at"] = datetime.utcnow().isoformat()
