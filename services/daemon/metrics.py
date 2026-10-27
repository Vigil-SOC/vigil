"""Metrics collection for daemon operations.

MetricsServer runs two listeners: health JSON (/health, /status) on
DAEMON_HEALTH_PORT (default 9091), and, when DAEMON_METRICS_ENABLED,
Prometheus text on DAEMON_METRICS_PORT (default 9090) at /metrics. The
Prometheus listener renders prometheus_client's default REGISTRY, which is
where the OTEL PrometheusMetricReader from core/telemetry.init_telemetry()
registers its collector — so the OTEL instruments appear there when the
flag is on.
"""

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.agents.queue import RUN_QUEUE
from core.config import get_settings
from core.llm.gateway.gateway import QUEUE_NAME
from core.redis_client import configured_redis_url, open_sync_redis, redact_logged_text
from core.telemetry import get_meter
from core.time import utcnow
from services.daemon.config import MetricsConfig
from services.daemon.vendor_errors import vendor_error_snapshot

logger = logging.getLogger(__name__)

DAEMON_HEALTH_PORT = get_settings().daemon_health_port
DAEMON_METRICS_PORT = get_settings().daemon_metrics_port

# A few seconds, not ARQ's retry budget: /status must not stall the listener.
_REDIS_SOCKET_TIMEOUT_S = 3

# Legacy poll loops. Webhook has no PollState. All seven are emitted even
# when that loop was never started, so a missing key means an older Vigil.
_LEGACY_POLL_SOURCES = (
    "splunk",
    "crowdstrike",
    "azure_sentinel",
    "aws_security_hub",
    "microsoft_defender",
    "elastic",
    "opensearch",
)

_FEDERATION_ZERO = {"polls": 0, "findings": 0, "errors": 0, "dropped": 0}


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
    (default 9090) when ``config.enabled``. Kept on separate ports so the
    scrape target and the probe target stay distinct.
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
        """Run the health server, and Prometheus when enabled, until shutdown."""
        health_app = web.Application()
        health_app.router.add_get("/health", self._handle_health)
        health_app.router.add_get("/status", self._handle_status)
        listeners = [(health_app, self.health_port, "Health")]

        if self.config.enabled:
            metrics_app = web.Application()
            metrics_app.router.add_get("/metrics", self._handle_metrics)
            listeners.append((metrics_app, self.metrics_port, "Prometheus"))

        runners = []
        try:
            for app, port, label in listeners:
                runner = web.AppRunner(app)
                await runner.setup()
                runners.append(runner)
                logger.info("%s server starting on port %d", label, port)
                await web.TCPSite(runner, get_settings().daemon_bind_host, port).start()

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
            "federation": metrics["federation"],
            "llm_queue_depth": metrics["llm_queue_depth"],
            "agent_runs_depth": metrics["agent_runs_depth"],
            "vendors": metrics["vendors"],
        }

        return web.json_response(status)

    def _collect_metrics(self) -> Dict[str, Any]:
        """Collect metrics from all component stats dicts."""
        metrics: Dict[str, Any] = {"vendors": vendor_error_snapshot()}

        poller_stats = self.poller.stats.copy() if self.poller else {}
        poller_stats["sources"] = _legacy_poll_sources(self.poller)
        metrics["poller"] = poller_stats

        if self.kafka_ingestor:
            metrics["kafka"] = dict(self.kafka_ingestor.stats)

        processor_stats = self.processor.stats.copy() if self.processor else {}
        processor_stats.update(_processor_pipeline(self.processor))
        metrics["processor"] = processor_stats

        responder_stats = self.responder.stats.copy() if self.responder else {}
        responder_stats["queue_depth"] = _queue_depth(self.responder)
        metrics["responder"] = responder_stats

        if self.scheduler:
            metrics["scheduler"] = self.scheduler.stats.copy()

        if self.orchestrator:
            orch_stats = self.orchestrator.stats.copy()
            orch_stats["active_agents"] = self.orchestrator._in_flight()
            orch_stats["enabled"] = self.orchestrator.enabled
        else:
            orch_stats = {}
        orch_stats.update(_orchestrator_capacity(self.orchestrator))
        metrics["orchestrator"] = orch_stats

        metrics["federation"] = _federation_totals(self.poller)
        llm_depth, runs_depth = _redis_queue_depths()
        metrics["llm_queue_depth"] = llm_depth
        metrics["agent_runs_depth"] = runs_depth

        return metrics


def _legacy_poll_sources(poller: Any) -> Dict[str, Any]:
    sources: Dict[str, Any] = {}
    for source_id in _LEGACY_POLL_SOURCES:
        state = getattr(poller, f"_{source_id}_state", None) if poller else None
        last = getattr(state, "last_poll_time", None)
        sources[source_id] = {"last_poll_time": last.isoformat() if last else None}
    return sources


def _processor_pipeline(processor: Any) -> Dict[str, Any]:
    if processor is None:
        return {
            "queue_depth": 0,
            "queue_max": 0,
            "enrich_inflight": 0,
            "enrich_breaker_tripped": False,
            "enrich_paused_until": None,
            "auto_triage_enabled": False,
        }
    # Imported here: processor -> probes -> probe_metrics, which this module
    # defines, so a top-level import is a cycle.
    from services.daemon.processor import INPUT_QUEUE_MAXSIZE

    return {
        "queue_depth": processor.input_queue.qsize(),
        "queue_max": INPUT_QUEUE_MAXSIZE,
        "enrich_inflight": len(getattr(processor, "_enrich_tasks", ())),
        "enrich_breaker_tripped": bool(
            getattr(processor, "_enrich_breaker_tripped", False)
        ),
        "enrich_paused_until": _enrich_paused_until(processor),
        "auto_triage_enabled": bool(
            getattr(getattr(processor, "config", None), "auto_triage_enabled", False)
        ),
    }


def _enrich_paused_until(processor: Any) -> Optional[str]:
    """ISO-8601 wall clock, or null unless the breaker is open and the
    monotonic deadline is still ahead. Recovery clears the flag and leaves
    the deadline where it is."""
    if not getattr(processor, "_enrich_breaker_tripped", False):
        return None
    remaining = getattr(processor, "_enrich_paused_until", 0.0) - time.monotonic()
    if remaining <= 0:
        return None
    return (utcnow() + timedelta(seconds=remaining)).isoformat()


def _queue_depth(component: Any) -> int:
    if component is None:
        return 0
    queue = getattr(component, "input_queue", None)
    if queue is None:
        return 0
    return queue.qsize()


def _orchestrator_capacity(orchestrator: Any) -> Dict[str, int]:
    """Free slots follow the admission gate (``_running``), not ``_in_flight``.

    A failed ``_running`` already returns the cap, so free slots are 0.
    """
    if orchestrator is None:
        return {"max_concurrent_agents": 0, "free_slots": 0}
    cap = int(
        getattr(getattr(orchestrator, "config", None), "max_concurrent_agents", 0)
    )
    running = getattr(orchestrator, "_running", None)
    held = running() if callable(running) else cap
    return {"max_concurrent_agents": cap, "free_slots": max(0, cap - int(held))}


def _federation_totals(poller: Any) -> Dict[str, int]:
    runner = getattr(poller, "_federation", None) if poller else None
    if runner is None:
        return dict(_FEDERATION_ZERO)
    return {
        "polls": int(getattr(runner, "polls", 0)),
        "findings": int(getattr(runner, "findings", 0)),
        "errors": int(getattr(runner, "errors", 0)),
        "dropped": int(getattr(runner, "dropped", 0)),
    }


def _redis_queue_depths() -> Tuple[Optional[int], Optional[int]]:
    """ZCARD of the ARQ llm queue and LLEN of the BullMQ wait list.

    Null when no Redis URL is configured or the command fails. A cached
    count would report a backlog that is already gone.
    """
    if configured_redis_url() is None:
        return None, None
    try:
        with open_sync_redis(
            socket_timeout=_REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_S,
        ) as client:
            llm = client.zcard(QUEUE_NAME)
            runs = client.llen(f"bull:{RUN_QUEUE}:wait")
        return int(llm), int(runs)
    except Exception as exc:
        logger.debug(
            "status queue depths unavailable: %s", redact_logged_text(str(exc))
        )
        return None, None
