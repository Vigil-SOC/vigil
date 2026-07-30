import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from aiohttp import web

from daemon.config import MetricsConfig

logger = logging.getLogger(__name__)

DAEMON_HEALTH_PORT = int(os.getenv("DAEMON_HEALTH_PORT", "9091"))


def _emit(instrument: Any, value: Any, attrs: Optional[Dict[str, str]] = None) -> None:
    """Record to an OTEL counter or histogram when one exists; never raises."""
    if instrument is None:
        return
    try:
        emit = getattr(instrument, "add", None) or instrument.record
        emit(value, attrs or {})
    except Exception as _err:
        logger.debug("OTEL emit failed (non-fatal): %s", _err)


class DaemonMetrics:
    def __init__(self):
        self._start_time = datetime.now(timezone.utc)
        self._poll_counts: Dict[str, int] = defaultdict(int)
        self._poll_durations: Dict[str, list] = defaultdict(list)
        self._events_counts: Dict[str, int] = defaultdict(int)
        self._processing_count: int = 0
        self._processing_durations: list = []
        self._polls_counter = None
        self._events_counter = None
        self._poll_duration_hist = None
        self._processed_counter = None
        self._processing_duration_hist = None

        try:
            from core.telemetry import get_meter
            meter = get_meter("vigil.daemon")

            self._polls_counter = meter.create_counter(
                name="soc_daemon_poller_polls_total",
                description="Total number of polls per source",
                unit="1",
            )
            self._events_counter = meter.create_counter(
                name="soc_daemon_poller_findings_total",
                description="Total findings/events retrieved per source",
                unit="1",
            )
            self._poll_duration_hist = meter.create_histogram(
                name="soc_daemon_poller_duration_seconds",
                description="Poll duration in seconds",
                unit="s",
            )
            self._processed_counter = meter.create_counter(
                name="soc_daemon_processor_processed_total",
                description="Total findings processed",
                unit="1",
            )
            self._processing_duration_hist = meter.create_histogram(
                name="soc_daemon_processor_duration_seconds",
                description="Processing batch duration in seconds",
                unit="s",
            )
        except Exception as _err:
            logger.debug("OTEL instruments unavailable, using in-memory only: %s", _err)

    def record_poll(self, source: str, duration: float, events_count: int):
        attrs = {"source": source}

        self._poll_counts[source] += 1
        self._poll_durations[source].append(duration)
        self._events_counts[source] += events_count

        _emit(self._polls_counter, 1, attrs)
        _emit(self._events_counter, events_count, attrs)
        _emit(self._poll_duration_hist, duration, attrs)

        logger.debug(
            "Recorded poll for %s: %d events in %.2fs", source, events_count, duration
        )

    def get_poll_count(self, source: str) -> int:

        return self._poll_counts.get(source, 0)

    def record_processing(self, findings_count: int, duration: float):
        self._processing_count += findings_count
        self._processing_durations.append(duration)

        _emit(self._processed_counter, findings_count)
        _emit(self._processing_duration_hist, duration)

        logger.debug(
            "Recorded processing: %d findings in %.2fs", findings_count, duration
        )

    def get_total_processed(self) -> int:
        """Get total number of findings processed."""
        return self._processing_count

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics (used for /status display only)."""
        uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        total_polls = sum(self._poll_counts.values())

        poll_stats = {}
        for source, durations in self._poll_durations.items():
            avg_duration = sum(durations) / len(durations) if durations else 0
            poll_stats[source] = {
                "count": self._poll_counts[source],
                "events": self._events_counts[source],
                "avg_duration": avg_duration,
            }

        processing_avg = (
            sum(self._processing_durations) / len(self._processing_durations)
            if self._processing_durations
            else 0
        )

        return {
            "uptime_seconds": uptime,
            "total_polls": total_polls,
            "total_processed": self._processing_count,
            "polls": poll_stats,
            "processing": {
                "total_processed": self._processing_count,
                "avg_duration": processing_avg,
                "batch_count": len(self._processing_durations),
            },
        }

    def reset(self):
        self._poll_counts.clear()
        self._poll_durations.clear()
        self._events_counts.clear()
        self._processing_count = 0
        self._processing_durations.clear()
        self._start_time = datetime.now(timezone.utc)
        logger.info("Metrics reset")


class MetricsServer:
    def __init__(self, config: MetricsConfig):
        self.config = config
        self._start_time = datetime.now(timezone.utc)
        self.poller = None
        self.kafka_ingestor = None
        self.processor = None
        self.responder = None
        self.scheduler = None
        self.orchestrator = None

    @property
    def _health_port(self) -> int:
        return DAEMON_HEALTH_PORT

    def _uptime(self) -> float:
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()

    async def run(self, shutdown_event: asyncio.Event):
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._health_port)

        logger.info("Health server starting on port %d", self._health_port)
        await site.start()

        await shutdown_event.wait()

        await runner.cleanup()
        logger.info("Health server stopped")

    async def _handle_health(self, request: web.Request) -> web.Response:
        health: Dict[str, Any] = {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": self._uptime(),
        }

        parts = self._components()
        parts.pop("kafka")  # optional ingested, its absence is not a health signal
        components = {
            name: "running" if c else "not_initialized" for name, c in parts.items()
        }
        if self.orchestrator and not self.orchestrator.enabled:
            components["orchestrator"] = "disabled"

        health["components"] = components

        if all(v == "running" for v in components.values()):
            health["status"] = "healthy"
        elif any(v == "running" for v in components.values()):
            health["status"] = "degraded"
        else:
            health["status"] = "unhealthy"

        status_code = 200 if health["status"] != "unhealthy" else 503
        return web.json_response(health, status=status_code)

    async def _handle_status(self, request: web.Request) -> web.Response:

        metrics = self._collect_metrics()

        status = {
            "daemon": {
                "start_time": self._start_time.isoformat(),
                "uptime_seconds": self._uptime(),
            },
            "poller": metrics.get("poller", {}),
            "kafka": metrics.get("kafka", {}),
            "processor": metrics.get("processor", {}),
            "responder": metrics.get("responder", {}),
            "scheduler": metrics.get("scheduler", {}),
            "orchestrator": metrics.get("orchestrator", {}),
        }

        return web.json_response(status)

    def _components(self) -> Dict[str, Any]:
        """The daemon subsystems this server reports on, in report order."""
        return {
            "poller": self.poller,
            "kafka": self.kafka_ingestor,
            "processor": self.processor,
            "responder": self.responder,
            "scheduler": self.scheduler,
            "orchestrator": self.orchestrator,
        }

    def _collect_metrics(self) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            name: dict(c.stats) for name, c in self._components().items() if c
        }

        if self.orchestrator:
            metrics["orchestrator"]["active_agents"] = (
                self.orchestrator.agent_runner.active_count
            )
            metrics["orchestrator"]["enabled"] = self.orchestrator.enabled

        return metrics
