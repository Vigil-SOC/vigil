"""Metrics collection for daemon operations.

MetricsServer runs two listeners: health JSON (/health, /status) on
DAEMON_HEALTH_PORT (default 9091), and Prometheus text on
DAEMON_METRICS_PORT (default 9090) at /metrics. The Prometheus listener
renders prometheus_client's default REGISTRY, which is where the OTEL
PrometheusMetricReader from core/telemetry.init_telemetry() registers its
collector — so the OTEL instruments appear there when the flag is on.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, Optional

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.config import get_settings
from core.telemetry import get_meter
from core.time import utcnow
from services.daemon.config import MetricsConfig
from services.daemon.vendor_errors import vendor_error_snapshot

logger = logging.getLogger(__name__)

DAEMON_HEALTH_PORT = get_settings().daemon_health_port
DAEMON_METRICS_PORT = get_settings().daemon_metrics_port


# ---------------------------------------------------------------------------
# ProbeMetrics — known-answer probe scores (#924)
# ---------------------------------------------------------------------------


class ProbeMetrics:
    """The two probe instruments.

    Instruments are created on first record rather than at import, so they
    bind to the real meter once ``init_telemetry`` has run and to the no-op
    one when it has not. These names are what the Grafana twin and the
    health screen (#887) query; they are not aliased to ``soc_daemon_*``.
    """

    def __init__(self):
        self._results_counter = None
        self._time_to_verdict_hist = None
        self._instruments_ready = False
        # In-memory shadow, keyed (probe, outcome).
        self.results: Dict[tuple, int] = defaultdict(int)

    def _ensure_instruments(self):
        if self._instruments_ready:
            return
        self._instruments_ready = True
        try:
            meter = get_meter("vigil.daemon")
            self._results_counter = meter.create_counter(
                name="vigil.probe.results.total",
                description="Known-answer probe scores by probe and outcome",
                unit="1",
            )
            self._time_to_verdict_hist = meter.create_histogram(
                name="vigil.probe.time_to_verdict.seconds",
                description="Seconds from probe creation to daemon triage verdict",
                unit="s",
            )
        except Exception as _err:
            logger.debug("OTEL probe instruments unavailable: %s", _err)

    def record(self, probe: str, outcome: str, time_to_verdict_s: Optional[float]):
        """Count one score; the histogram only sees hit/miss (a verdict exists)."""
        self.results[(probe, outcome)] += 1
        self._ensure_instruments()
        try:
            if self._results_counter is not None:
                self._results_counter.add(1, {"probe": probe, "outcome": outcome})
            if time_to_verdict_s is not None and self._time_to_verdict_hist is not None:
                self._time_to_verdict_hist.record(time_to_verdict_s, {"probe": probe})
        except Exception as _err:
            logger.debug("OTEL probe record failed (non-fatal): %s", _err)


probe_metrics = ProbeMetrics()


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

        self._tasks: Dict[str, asyncio.Task] = {}

    def register_task(self, name: str, task: "asyncio.Task") -> None:
        """Track a component's task so /health reflects whether it is alive."""
        self._tasks[name] = task

    def _component_state(self, name: str, component: Any) -> str:
        task = self._tasks.get(name)
        if component is None or task is None:
            return "not_initialized"
        # done() first: a dead orchestrator task is a failure even when disabled.
        if task.done():
            if task.cancelled():
                return "stopped"
            exc = task.exception()
            return f"failed: {type(exc).__name__}" if exc else "stopped"
        if name == "orchestrator" and not component.enabled:
            return "disabled"
        return "running"

    @property
    def health_port(self) -> int:
        return DAEMON_HEALTH_PORT

    @property
    def metrics_port(self) -> int:
        return DAEMON_METRICS_PORT

    async def run(self, shutdown_event: asyncio.Event):
        """Run the health and Prometheus HTTP servers until shutdown."""
        health_app = web.Application()
        health_app.router.add_get("/health", self._handle_health)
        health_app.router.add_get("/status", self._handle_status)

        metrics_app = web.Application()
        metrics_app.router.add_get("/metrics", self._handle_metrics)

        runners = []
        try:
            for app, port, label in (
                (health_app, self.health_port, "Health"),
                (metrics_app, self.metrics_port, "Prometheus"),
            ):
                runner = web.AppRunner(app)
                await runner.setup()
                runners.append(runner)
                logger.info("%s server starting on port %d", label, port)
                await web.TCPSite(runner, "0.0.0.0", port).start()

            await shutdown_event.wait()
        except Exception:
            # A failed second bind must not leave the first listener orphaned;
            # log here because main.py gathers with return_exceptions=True.
            logger.exception("Health/Prometheus server failed")
            raise
        finally:
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

        components = {
            name: self._component_state(name, obj)
            for name, obj in (
                ("poller", self.poller),
                ("kafka", self.kafka_ingestor),
                ("processor", self.processor),
                ("responder", self.responder),
                ("scheduler", self.scheduler),
                ("orchestrator", self.orchestrator),
            )
        }
        health["components"] = components

        # A dead task makes the daemon unhealthy so the probes restart it.
        ok = all(v in ("running", "disabled") for v in components.values())
        health["status"] = "healthy" if ok else "unhealthy"
        status_code = 200 if ok else 503
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
            "vendors": metrics["vendors"],
        }

        return web.json_response(status)

    def _collect_metrics(self) -> Dict[str, Any]:
        """Collect metrics from all component stats dicts."""
        metrics: Dict[str, Any] = {"vendors": vendor_error_snapshot()}

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
