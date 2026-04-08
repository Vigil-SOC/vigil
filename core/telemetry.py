"""
core/telemetry.py — OpenTelemetry bootstrap for Vigil SOC.

When VIGIL_OTEL_ENABLED is falsy OR the SDK cannot be imported, every
public function returns a no-op object. Telemetry failures never crash
the application.

Environment variables:
    VIGIL_OTEL_ENABLED              Master switch ("true"/"1"/"yes" to enable)
    OTEL_EXPORTER_OTLP_ENDPOINT     Collector address (default http://localhost:4317)
    VIGIL_OTEL_RECORD_LLM_CONTENT   Opt-in to recording LLM prompts/responses (default off)
    VIGIL_OTEL_RECORD_IOC_VALUES    Opt-in to recording raw finding/IOC content (default off)
    ENVIRONMENT                     Deployment environment label (default "development")
    RELEASE_VERSION                 Service version label (default "unknown")
"""
from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_initialized: bool = False
_tracer_provider: Any = None
_meter_provider: Any = None

# Context variable for investigation correlation across async tasks
_investigation_id_var: ContextVar[Optional[str]] = ContextVar(
    "vigil_investigation_id", default=None
)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def set_investigation_id(investigation_id: Optional[str]) -> None:
    """Attach an investigation ID to the current async context."""
    _investigation_id_var.set(investigation_id)


def get_investigation_id() -> Optional[str]:
    """Return the investigation ID bound to the current async context."""
    return _investigation_id_var.get()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _is_otel_enabled() -> bool:
    val = os.environ.get("VIGIL_OTEL_ENABLED", "").lower()
    return val in ("true", "1", "yes")


def _should_record_llm_content() -> bool:
    """Return True only when the operator has explicitly opted in."""
    val = os.environ.get("VIGIL_OTEL_RECORD_LLM_CONTENT", "").lower()
    return val in ("true", "1", "yes")


def _should_record_ioc_values() -> bool:
    """Return True only when the operator has explicitly opted in."""
    val = os.environ.get("VIGIL_OTEL_RECORD_IOC_VALUES", "").lower()
    return val in ("true", "1", "yes")


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

def _do_init(service_name: str) -> None:
    """Actually initialize OTEL providers. Raises on any failure."""
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    global _tracer_provider, _meter_provider

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    environment = os.environ.get("ENVIRONMENT", "development")
    version = os.environ.get("RELEASE_VERSION", "unknown")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version,
            "deployment.environment": environment,
            "vigil.component": service_name,
        }
    )

    # --- TracerProvider ---
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
    sentry_dsn = os.environ.get("SENTRY_DSN", "")
    if sentry_dsn:
        try:
            from backend.monitoring import SentrySpanProcessor
            tracer_provider.add_span_processor(SentrySpanProcessor())
        except Exception:
            pass  # backend.monitoring may not be importable in daemon context

    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    # --- MeterProvider ---
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        metric_reader = PrometheusMetricReader()
        logger.debug("Using PrometheusMetricReader on port 9090")
    except Exception:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True)
            )
        except Exception:
            metric_reader = None

    meter_kwargs: dict = {"resource": resource}
    if metric_reader is not None:
        meter_kwargs["metric_readers"] = [metric_reader]

    meter_provider = MeterProvider(**meter_kwargs)
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    logger.info(
        "OpenTelemetry initialized for service '%s' → %s", service_name, endpoint
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_telemetry(service_name: str) -> bool:
    """
    Bootstrap tracing and metrics for a process.

    Safe to call multiple times — subsequent calls are no-ops if already
    initialized. Returns True if OTEL was actually enabled and initialized.
    """
    global _initialized

    if not _is_otel_enabled():
        logger.debug("VIGIL_OTEL_ENABLED is not set — telemetry disabled")
        return False

    if _initialized:
        return True

    try:
        _do_init(service_name)
        _initialized = True
        _install_json_logging()
        return True
    except Exception as exc:
        logger.warning(
            "OpenTelemetry initialization failed (non-fatal): %s", exc
        )
        return False


def get_tracer(name: str) -> Any:
    """Return a tracer. Always safe to call — returns a no-op when disabled."""
    if _initialized:
        try:
            from opentelemetry import trace

            return trace.get_tracer(name)
        except Exception:
            pass
    return _FallbackNoOpTracer()


def get_meter(name: str) -> Any:
    """Return a meter. Always safe to call — returns a no-op when disabled."""
    if _initialized:
        try:
            from opentelemetry import metrics

            return metrics.get_meter(name)
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
    if _initialized:
        try:
            from opentelemetry.propagators.textmap import TraceContextTextMapPropagator

            TraceContextTextMapPropagator().inject(carrier)
        except Exception:
            pass
    return carrier


def extract_traceparent(carrier: dict) -> Any:
    """
    Extract W3C trace context from *carrier* and return an OTEL Context.
    Returns None when disabled or on any error.
    """
    if _initialized and carrier:
        try:
            from opentelemetry.propagators.textmap import TraceContextTextMapPropagator

            return TraceContextTextMapPropagator().extract(carrier)
        except Exception:
            pass
    return None


def create_genai_metrics(meter: Any) -> dict:
    """
    Create and return the 4 GenAI metric instruments used for LLM observability.

    Keys: llm_calls, llm_duration, llm_tokens, llm_cost_usd
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
    }


def shutdown() -> None:
    """
    Flush and shut down all telemetry providers.

    Safe to call even when OTEL was never initialized.
    After shutdown, init_telemetry() may be called again.
    """
    global _initialized, _tracer_provider, _meter_provider

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
    _initialized = False  # reset so init_telemetry() can be called again


# Alias used in the plan and by callers that prefer the longer name
shutdown_telemetry = shutdown


# ---------------------------------------------------------------------------
# Structured JSON logging with OTEL trace correlation (#59)
# ---------------------------------------------------------------------------

def _install_json_logging() -> None:
    """
    Replace the root logger's handlers with structured JSON output.

    Each log line includes trace_id and span_id from the current OTEL span
    so that logs can be correlated with traces in Jaeger/Grafana.
    Retains file output alongside stdout.
    """

    class _OTELJsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            trace_id = ""
            span_id = ""
            try:
                from opentelemetry import trace

                span = trace.get_current_span()
                ctx = span.get_span_context()
                if ctx and ctx.is_valid:
                    trace_id = format(ctx.trace_id, "032x")
                    span_id = format(ctx.span_id, "016x")
            except Exception:
                pass

            entry: dict = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "trace_id": trace_id,
                "span_id": span_id,
            }

            inv_id = get_investigation_id()
            if inv_id:
                entry["vigil.investigation.id"] = inv_id

            if record.exc_info:
                entry["exception"] = self.formatException(record.exc_info)

            for key, val in record.__dict__.items():
                if key.startswith("vigil."):
                    entry[key] = val

            return json.dumps(entry, default=str)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    formatter = _OTELJsonFormatter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = Path.home() / ".deeptempo"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "vigil.log")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as exc:
        logger.warning("Could not set up file logging (non-fatal): %s", exc)
