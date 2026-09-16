"""Metrics collection for daemon operations.

DaemonMetrics is a thin wrapper around OpenTelemetry instruments.  It
preserves the existing public interface (record_poll, record_processing,
get_summary, reset, get_poll_count, get_total_processed) so all callers
(poller.py, processor.py, responder.py, scheduler.py) need zero changes.

MetricsServer runs two listeners: health JSON (/health, /status) on
DAEMON_HEALTH_PORT (default 9091), and Prometheus text on
DAEMON_METRICS_PORT (default 9090) at /metrics. The Prometheus listener
renders prometheus_client's default REGISTRY, which is where the OTEL
PrometheusMetricReader from core/telemetry.init_telemetry() registers its
collector — so the OTEL instruments above appear there when the flag is on.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.config import get_settings
from core.time import utcnow
from services.daemon.config import MetricsConfig

logger = logging.getLogger(__name__)

DAEMON_HEALTH_PORT = get_settings().daemon_health_port
DAEMON_METRICS_PORT = get_settings().daemon_metrics_port


# ---------------------------------------------------------------------------
# DaemonMetrics — OTEL-backed, public interface unchanged
# ---------------------------------------------------------------------------


class DaemonMetrics:
    """Metrics tracking backed by OpenTelemetry instruments.

    Falls back to in-memory counters if OTEL is unavailable so the daemon
    boots cleanly even without telemetry configured.
    """

    def __init__(self):
        self._start_time = utcnow()

        # In-memory shadow counters (used by get_summary / get_poll_count /
        # get_total_processed which must return values synchronously).
        self._poll_counts: Dict[str, int] = defaultdict(int)
        self._poll_durations: Dict[str, list] = defaultdict(list)
        self._events_counts: Dict[str, int] = defaultdict(int)
        self._processing_count: int = 0
        self._processing_durations: list = []

        # OTEL instruments — created lazily; None if OTEL not available.
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

    # ------------------------------------------------------------------
    # Public interface (preserved exactly)
    # ------------------------------------------------------------------

    def record_poll(self, source: str, duration: float, events_count: int):
        """Record a poll operation."""
        attrs = {"source": source}

        # Shadow counters
        self._poll_counts[source] += 1
        self._poll_durations[source].append(duration)
        self._events_counts[source] += events_count

        # OTEL
        try:
            if self._polls_counter is not None:
                self._polls_counter.add(1, attrs)
            if self._events_counter is not None:
                self._events_counter.add(events_count, attrs)
            if self._poll_duration_hist is not None:
                self._poll_duration_hist.record(duration, attrs)
        except Exception as _err:
            logger.debug("OTEL record_poll failed (non-fatal): %s", _err)

        logger.debug(
            "Recorded poll for %s: %d events in %.2fs", source, events_count, duration
        )

    def get_poll_count(self, source: str) -> int:
        """Get total poll count for a source."""
        return self._poll_counts.get(source, 0)

    def record_processing(self, findings_count: int, duration: float):
        """Record findings processing operation."""
        self._processing_count += findings_count
        self._processing_durations.append(duration)

        try:
            if self._processed_counter is not None:
                self._processed_counter.add(findings_count)
            if self._processing_duration_hist is not None:
                self._processing_duration_hist.record(duration)
        except Exception as _err:
            logger.debug("OTEL record_processing failed (non-fatal): %s", _err)

        logger.debug(
            "Recorded processing: %d findings in %.2fs", findings_count, duration
        )

    def get_total_processed(self) -> int:
        """Get total number of findings processed."""
        return self._processing_count

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics (used for /status display only)."""
        uptime = (utcnow() - self._start_time).total_seconds()
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
        """Reset in-memory shadow counters (OTEL instruments are cumulative by design)."""
        self._poll_counts.clear()
        self._poll_durations.clear()
        self._events_counts.clear()
        self._processing_count = 0
        self._processing_durations.clear()
        self._start_time = utcnow()
        logger.info("Metrics reset")


# ---------------------------------------------------------------------------
# MetricsServer — health/status on DAEMON_HEALTH_PORT, Prometheus on
# DAEMON_METRICS_PORT
# ---------------------------------------------------------------------------


class MetricsServer:
    """Health/status and Prometheus HTTP listeners for the daemon.

    /health and /status (JSON) on DAEMON_HEALTH_PORT (default 9091);
    /metrics (Prometheus text, default registry) on DAEMON_METRICS_PORT
    (default 9090). Kept on separate ports so the scrape target and the
    probe target stay distinct.
    """

    def __init__(self, config: MetricsConfig):
        self.config = config
        self._start_time = utcnow()

        # Component references (set externally)
        self.poller = None
        self.kafka_ingestor = None
        self.processor = None
        self.responder = None
        self.scheduler = None
        self.orchestrator = None

    @property
    def _health_port(self) -> int:
        return DAEMON_HEALTH_PORT

    @property
    def _metrics_port(self) -> int:
        return DAEMON_METRICS_PORT

    async def run(self, shutdown_event: asyncio.Event):
        """Run the health and Prometheus HTTP servers until shutdown."""
        health_app = web.Application()
        health_app.router.add_get("/health", self._handle_health)
        health_app.router.add_get("/status", self._handle_status)

        metrics_app = web.Application()
        metrics_app.router.add_get("/metrics", self._handle_metrics)

        runners = []
        for app, port, label in (
            (health_app, self._health_port, "Health"),
            (metrics_app, self._metrics_port, "Prometheus"),
        ):
            runner = web.AppRunner(app)
            await runner.setup()
            runners.append(runner)
            logger.info("%s server starting on port %d", label, port)
            await web.TCPSite(runner, "0.0.0.0", port).start()

        await shutdown_event.wait()

        for runner in runners:
            await runner.cleanup()
        logger.info("Health and Prometheus servers stopped")

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus text for the default registry (OTEL reader lives there)."""
        # Header set raw: aiohttp's content_type= rejects the charset parameter
        # that CONTENT_TYPE_LATEST carries.
        return web.Response(
            body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle health check request."""
        health: Dict[str, Any] = {
            "status": "healthy",
            "timestamp": utcnow().isoformat(),
            "uptime_seconds": (utcnow() - self._start_time).total_seconds(),
        }

        components = {}

        if self.poller:
            components["poller"] = "running"
        else:
            components["poller"] = "not_initialized"

        if self.processor:
            components["processor"] = "running"
        else:
            components["processor"] = "not_initialized"

        if self.responder:
            components["responder"] = "running"
        else:
            components["responder"] = "not_initialized"

        if self.scheduler:
            components["scheduler"] = "running"
        else:
            components["scheduler"] = "not_initialized"

        if self.orchestrator:
            components["orchestrator"] = (
                "running" if self.orchestrator.enabled else "disabled"
            )
        else:
            components["orchestrator"] = "not_initialized"

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
        """Handle detailed status request."""
        metrics = self._collect_metrics()

        status = {
            "daemon": {
                "start_time": self._start_time.isoformat(),
                "uptime_seconds": (utcnow() - self._start_time).total_seconds(),
            },
            "poller": metrics.get("poller", {}),
            "kafka": metrics.get("kafka", {}),
            "processor": metrics.get("processor", {}),
            "responder": metrics.get("responder", {}),
            "scheduler": metrics.get("scheduler", {}),
            "orchestrator": metrics.get("orchestrator", {}),
        }

        return web.json_response(status)

    def _collect_metrics(self) -> Dict[str, Any]:
        """Collect metrics from all component stats dicts."""
        metrics: Dict[str, Any] = {}

        if self.poller:
            metrics["poller"] = self.poller.stats.copy()

        if self.kafka_ingestor:
            metrics["kafka"] = dict(self.kafka_ingestor.stats)

        if self.processor:
            metrics["processor"] = self.processor.stats.copy()

        if self.responder:
            metrics["responder"] = self.responder.stats.copy()

        if self.scheduler:
            metrics["scheduler"] = self.scheduler.stats.copy()

        if self.orchestrator:
            orch_stats = self.orchestrator.stats.copy()
            orch_stats["active_agents"] = self.orchestrator._in_flight()
            orch_stats["enabled"] = self.orchestrator.enabled
            metrics["orchestrator"] = orch_stats

        return metrics
