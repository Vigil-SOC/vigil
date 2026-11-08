"""
core/telemetry.py — OpenTelemetry bootstrap for Vigil SOC.

Metrics are always on: the Prometheus reader behind /metrics is a local pull.
Traces and OTLP metric export start only when OTEL_EXPORTER_OTLP_ENDPOINT is
set; otherwise the tracer helpers are no-ops. If the SDK cannot be imported
every public function returns a no-op object. Telemetry failures never crash
the application. Span schema: docs/telemetry.md.

Environment variables:
    OTEL_EXPORTER_OTLP_ENDPOINT     Collector address; empty (default) exports nothing
    ENVIRONMENT                     Deployment environment label (default "development")
    RELEASE_VERSION                 Service version label (default "unknown")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import get_settings, vigil_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_metrics_initialized: bool = False  # MeterProvider installed (always attempted)
_tracing_initialized: bool = False  # TracerProvider + OTLP export (endpoint set)
_tracer_provider: Any = None
_meter_provider: Any = None


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def otlp_endpoint() -> str:
    """The collector address, or "" when export is off. The one switch."""
    return (get_settings().otel_exporter_otlp_endpoint or "").strip()


# ---------------------------------------------------------------------------
# Fallback no-op classes (used when OTEL is disabled or SDK missing)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """No-op span satisfying the OpenTelemetry Span interface."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        pass

    def record_exception(
        self,
        exception: BaseException,
        attributes: Any = None,
        timestamp: Any = None,
        escaped: bool = False,
    ) -> None:
        pass

    def add_event(
        self, name: str, attributes: Any = None, timestamp: Any = None
    ) -> None:
        pass

    def end(self, end_time: Any = None) -> None:
        pass

    def is_recording(self) -> bool:
        """OTEL SDK defines is_recording as a callable method, not a property."""
        return False

    def get_span_context(self) -> Any:
        return None

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


class _FallbackNoOpTracer:
    """No-op tracer returned when OTEL is disabled or unavailable."""

    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpInstrument:
    """No-op metric instrument (counter, histogram, gauge, etc.)."""

    def add(self, amount: Any, attributes: Any = None) -> None:
        pass

    def record(self, amount: Any, attributes: Any = None) -> None:
        pass


class _FallbackNoOpMeter:
    """No-op meter returned when OTEL is disabled or unavailable."""

    def create_counter(self, name: str, **kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_histogram(self, name: str, **kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_observable_gauge(self, name: str, **kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_up_down_counter(self, name: str, **kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_observable_counter(self, name: str, **kwargs: Any) -> _NoOpInstrument:
        return _NoOpInstrument()

    def create_observable_up_down_counter(
        self, name: str, **kwargs: Any
    ) -> _NoOpInstrument:
        return _NoOpInstrument()


# ---------------------------------------------------------------------------
# Internal initialization
# ---------------------------------------------------------------------------


def _resource(service_name: str) -> Any:
    from opentelemetry.sdk.resources import Resource

    settings = get_settings()
    return Resource.create(
        {
            "service.name": service_name,
            "service.version": settings.release_version,
            "deployment.environment": settings.environment,
            "vigil.component": service_name,
        }
    )


def _init_tracing(service_name: str, endpoint: str) -> None:
    """Install the TracerProvider exporting to *endpoint*. Raises on any failure."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    global _tracer_provider

    resource = _resource(service_name)

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    except Exception:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPSpanExporterHTTP,
        )

        span_exporter = OTLPSpanExporterHTTP(endpoint=endpoint)

    batch_processor = BatchSpanProcessor(
        span_exporter,
        max_queue_size=2048,
        max_export_batch_size=512,
        schedule_delay_millis=5000,
        export_timeout_millis=30000,
    )

    from core.telemetry_sanitizer import SensitiveAttributeScrubber

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SensitiveAttributeScrubber())
    tracer_provider.add_span_processor(batch_processor)

    # Wire Sentry alongside OTEL when configured (prevents double-tracing)
    sentry_dsn = get_settings().sentry_dsn
    if sentry_dsn:
        from core.platform.monitoring import SentrySpanProcessor

        tracer_provider.add_span_processor(SentrySpanProcessor())

    # The global provider can be set once per process; after a shutdown and
    # re-init the new one is still what get_tracer() hands out via _tracer_provider.
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider


def _init_metrics(service_name: str, endpoint: str) -> None:
    """Install the MeterProvider. Raises on any failure."""
    from opentelemetry import metrics
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider

    global _meter_provider

    # The Prometheus reader registers its collector on prometheus_client's
    # default REGISTRY, which is what the backend /metrics route and the
    # daemon's :9090 listener serve. With an endpoint, an OTLP reader also
    # pushes to the collector so processes with no HTTP port (llm-worker)
    # still appear on the collector's Prometheus exporter.
    readers: list = []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True)
            )
        )
    # Last: constructing PrometheusMetricReader registers on REGISTRY, so
    # anything that can raise happens before that side effect.
    readers.insert(0, PrometheusMetricReader())
    meter_provider = MeterProvider(
        resource=_resource(service_name), metric_readers=readers
    )
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_telemetry(service_name: str) -> bool:
    """
    Bootstrap tracing and metrics for a process.

    Metrics always start; traces and OTLP export only when an endpoint is set.
    Safe to call multiple times — each half is skipped once initialized.
    Returns True if export to a collector is running.
    """
    global _metrics_initialized, _tracing_initialized

    endpoint = otlp_endpoint()
    if not _metrics_initialized:
        try:
            _init_metrics(service_name, endpoint)
            _metrics_initialized = True
        except Exception as exc:
            logger.warning("OpenTelemetry metrics failed to start (non-fatal): %s", exc)

    if not endpoint:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT is empty — nothing exported")
        return False
    if not _tracing_initialized:
        try:
            _init_tracing(service_name, endpoint)
            _tracing_initialized = True
            logger.info(
                "OpenTelemetry export for service '%s' → %s", service_name, endpoint
            )
        except Exception as exc:
            logger.warning("OpenTelemetry tracing failed to start (non-fatal): %s", exc)
    return _tracing_initialized


def get_tracer(name: str) -> Any:
    """Return a tracer. Always safe to call — returns a no-op when disabled."""
    if _tracing_initialized and _tracer_provider is not None:
        try:
            return _tracer_provider.get_tracer(name)
        except Exception:
            pass
    return _FallbackNoOpTracer()


def get_meter(name: str) -> Any:
    """Return a meter. Always safe to call — returns a no-op when disabled."""
    if _metrics_initialized and _meter_provider is not None:
        try:
            return _meter_provider.get_meter(name)
        except Exception:
            pass
    return _FallbackNoOpMeter()


def inject_traceparent(carrier: Optional[dict] = None) -> dict:
    """
    Inject the current W3C traceparent into *carrier* (mutates and returns it).
    Used to propagate trace context across async boundaries (e.g. ARQ jobs).
    """
    if carrier is None:
        carrier = {}
    if _tracing_initialized:
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            TraceContextTextMapPropagator().inject(carrier)
        except Exception:
            pass
    return carrier


def extract_traceparent(carrier: dict) -> Any:
    """
    Extract W3C trace context from *carrier* and return an OTEL Context.
    Returns None when disabled or on any error.
    """
    if _tracing_initialized and carrier:
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            return TraceContextTextMapPropagator().extract(carrier)
        except Exception:
            pass
    return None


def create_genai_metrics(meter: Any) -> dict:
    """
    Create and return the GenAI metric instruments used for LLM observability.

    Keys: llm_calls, llm_duration, llm_tokens, llm_cost_usd, llm_budget_unenforced
    """
    return {
        "llm_calls": meter.create_counter(
            "vigil.llm.calls.total",
            description="Total LLM API calls",
        ),
        "llm_duration": meter.create_histogram(
            "vigil.llm.duration.seconds",
            description="LLM call duration in seconds",
        ),
        "llm_tokens": meter.create_counter(
            "vigil.llm.tokens.total",
            description="Total LLM tokens consumed",
        ),
        "llm_cost_usd": meter.create_counter(
            "vigil.llm.cost.usd.total",
            description="Cumulative LLM cost in USD",
        ),
        "llm_budget_unenforced": meter.create_counter(
            "vigil.llm.budget.unenforced.total",
            description="LLM dispatches sent without a Bifrost virtual key (x-bf-vk)",
        ),
    }


# GenAI instruments, created on first record. Only cached once the real
# MeterProvider is installed: get_meter() before init_telemetry() returns
# _FallbackNoOpMeter, and caching that would silence the counters for the
# life of the process.
_genai_metrics: Optional[dict] = None


def record_llm_call(
    *,
    model: Optional[str],
    provider: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    duration_s: float,
    cost_usd: Optional[float],
) -> None:
    """
    Record one completed LLM call on the four GenAI instruments.

    Attributes are limited to ``model`` / ``provider`` (plus ``token_type`` on
    the token counter) — never prompt or response content. Runs on the
    request path, so it swallows every exception; it is a no-op when OTEL is
    disabled. An unpriced call (``cost_usd is None``) is still counted, but
    adds nothing to the cost instrument rather than a guessed $0.
    """
    global _genai_metrics
    try:
        # Normalise everything before touching any instrument so a bad value
        # skips the whole record rather than leaving the four out of step.
        tokens = {
            "input": int(input_tokens or 0),
            "output": int(output_tokens or 0),
            "cache_read": int(cache_read_tokens or 0),
            "cache_creation": int(cache_creation_tokens or 0),
        }
        duration = max(float(duration_s or 0.0), 0.0)
        cost = None if cost_usd is None else max(float(cost_usd), 0.0)
        attrs = {"model": model or "unknown", "provider": provider or "unknown"}

        metrics = _genai_metrics
        if metrics is None:
            meter = get_meter("vigil.llm")
            metrics = create_genai_metrics(meter)
            if _metrics_initialized and not isinstance(meter, _FallbackNoOpMeter):
                _genai_metrics = metrics

        metrics["llm_calls"].add(1, attrs)
        metrics["llm_duration"].record(duration, attrs)
        if cost is not None:
            metrics["llm_cost_usd"].add(cost, attrs)
        for token_type, count in tokens.items():
            if count:
                metrics["llm_tokens"].add(count, {**attrs, "token_type": token_type})
    except Exception:
        pass


def record_budget_unenforced(reason: str) -> None:
    """
    Count one LLM dispatch that went out without ``x-bf-vk``.

    ``reason`` is the budget enforcement status (``read_error``,
    ``not_configured``, ``dev_mode``, ``unlimited``). Never raises; a no-op when
    OTEL is disabled.
    """
    global _genai_metrics
    try:
        metrics = _genai_metrics
        if metrics is None:
            meter = get_meter("vigil.llm")
            metrics = create_genai_metrics(meter)
            if _metrics_initialized and not isinstance(meter, _FallbackNoOpMeter):
                _genai_metrics = metrics
        metrics["llm_budget_unenforced"].add(1, {"reason": reason})
    except Exception:
        pass


def shutdown() -> None:
    """
    Flush and shut down all telemetry providers.

    Safe to call even when OTEL was never initialized.
    After shutdown, init_telemetry() may be called again.
    """
    global _metrics_initialized, _tracing_initialized
    global _tracer_provider, _meter_provider, _genai_metrics

    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_millis=30000)
            _tracer_provider.shutdown()
        except Exception as exc:
            logger.warning("TracerProvider shutdown error (non-fatal): %s", exc)

    if _meter_provider is not None:
        try:
            _meter_provider.force_flush(timeout_millis=30000)
            _meter_provider.shutdown()
        except Exception as exc:
            logger.warning("MeterProvider shutdown error (non-fatal): %s", exc)

    _tracer_provider = None
    _meter_provider = None
    _genai_metrics = None  # instruments belong to the provider just shut down
    # reset so init_telemetry() can be called again
    _metrics_initialized = False
    _tracing_initialized = False


# Alias used in the plan and by callers that prefer the longer name
shutdown_telemetry = shutdown


# ---------------------------------------------------------------------------
# Structured JSON logging with OTEL trace correlation (#59)
# ---------------------------------------------------------------------------


def current_trace_ids() -> tuple[str, str]:
    """Hex (trace_id, span_id) of the active OTEL span, or empty strings."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return "", ""


TEXT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Libraries that log routine per-request/per-query chatter at INFO. Pinned so a
# root level of INFO shows Vigil's own records, not theirs.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "sqlalchemy.engine",
    "urllib3",
    "watchfiles",
    "asyncio",
)


class _OTELJsonFormatter(logging.Formatter):
    """One JSON object per record, with trace ids and a stable grouping key.

    ``msg_template`` (``record.msg`` before args are applied) plus ``logger``
    and ``exc_type`` identify a call site without regex-normalising
    ``message``. Neither carries argument values.
    """

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_trace_ids()

        entry: dict = {
            # formatTime goes through time.strftime, which has no %f.
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "msg_template": record.msg if isinstance(record.msg, str) else "",
            "trace_id": trace_id,
            "span_id": span_id,
        }

        if record.exc_info:
            exc_cls = record.exc_info[0]
            if exc_cls is not None:
                entry["exc_type"] = f"{exc_cls.__module__}.{exc_cls.__qualname__}"
            entry["exception"] = self.formatException(record.exc_info)

        for key, val in record.__dict__.items():
            if key.startswith("vigil."):
                entry[key] = val

        return json.dumps(entry, default=str)


def configure_logging(level: str | int = "INFO") -> None:
    """
    Install the process's root logging: stdout plus ``vigil.log`` in the State
    Directory, at *level*. Independent of OTEL; called by the backend, daemon
    and llm-worker on startup.

    ``VIGIL_LOG_FORMAT`` picks JSON (default; carries trace_id/span_id from the
    active OTEL span, empty when none) or the plain text format.
    """
    formatter: logging.Formatter = (
        logging.Formatter(TEXT_LOG_FORMAT)
        if get_settings().vigil_log_format == "text"
        else _OTELJsonFormatter()
    )

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = vigil_path(write=True)
        file_handler = logging.FileHandler(log_dir / "vigil.log")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as exc:
        logger.warning("Could not set up file logging (non-fatal): %s", exc)
