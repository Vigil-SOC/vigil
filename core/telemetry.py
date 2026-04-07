"""
OpenTelemetry bootstrap for Vigil SOC.

Call ``init_telemetry(service_name)`` once per process (backend, daemon,
llm-worker).  When ``VIGIL_OTEL_ENABLED`` is falsy **or** the SDK cannot
be imported, every public function in this module returns a no-op object
so callers never need to guard against ImportError.

Environment variables (all optional):
    VIGIL_OTEL_ENABLED          – master kill-switch, default "false"
    OTEL_EXPORTER_OTLP_ENDPOINT – collector address, default "http://localhost:4317"
    OTEL_SERVICE_NAME           – overrides the *service_name* argument
    OTEL_RESOURCE_ATTRIBUTES    – extra resource key=value pairs
    VIGIL_OTEL_RECORD_LLM_CONTENT – record full LLM prompts/responses, default "false"
    VIGIL_OTEL_RECORD_IOC_VALUES  – record IOC values in spans, default "false"
    VIGIL_OTEL_LOG_LEVEL        – log level for OTEL diagnostics, default "WARNING"
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variable for investigation correlation
# ---------------------------------------------------------------------------
_investigation_id_var: ContextVar[Optional[str]] = ContextVar(
    "vigil_investigation_id", default=None
)


def set_investigation_id(investigation_id: Optional[str]) -> None:
    """Set the current investigation ID for trace/log correlation."""
    _investigation_id_var.set(investigation_id)


def get_investigation_id() -> Optional[str]:
    """Get the current investigation ID (or None)."""
    return _investigation_id_var.get()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------
def _is_otel_enabled() -> bool:
    return os.getenv("VIGIL_OTEL_ENABLED", "false").lower() in ("true", "1", "yes")


def _should_record_llm_content() -> bool:
    return os.getenv("VIGIL_OTEL_RECORD_LLM_CONTENT", "false").lower() in (
        "true", "1", "yes",
    )


def _should_record_ioc_values() -> bool:
    return os.getenv("VIGIL_OTEL_RECORD_IOC_VALUES", "false").lower() in (
        "true", "1", "yes",
    )


# ---------------------------------------------------------------------------
# Module-level singletons (set by init_telemetry)
# ---------------------------------------------------------------------------
_tracer_provider = None
_meter_provider = None
_initialized = False


def _get_noop_tracer(name: str = __name__):
    """Return an OTEL no-op tracer (always available, zero overhead)."""
    try:
        from opentelemetry.trace import NoOpTracer
        return NoOpTracer()
    except ImportError:
        return _FallbackNoOpTracer()


def _get_noop_meter(name: str = __name__):
    """Return an OTEL no-op meter (always available, zero overhead)."""
    try:
        from opentelemetry.metrics import NoOpMeter
        return NoOpMeter(name)
    except ImportError:
        return _FallbackNoOpMeter()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def init_telemetry(
    service_name: str,
    service_version: str = "1.0.0",
) -> bool:
    """
    Bootstrap OTEL tracing, metrics, and logging for this process.

    Returns True if OTEL was initialised, False if running in no-op mode.
    Safe to call multiple times; second+ calls are ignored.
    """
    global _tracer_provider, _meter_provider, _initialized

    if _initialized:
        logger.debug("Telemetry already initialised, skipping")
        return _tracer_provider is not None

    _initialized = True

    if not _is_otel_enabled():
        logger.info("OTEL telemetry disabled (VIGIL_OTEL_ENABLED != true)")
        return False

    try:
        return _do_init(service_name, service_version)
    except Exception:
        # Telemetry must never crash the application.
        logger.warning("OTEL initialisation failed — running in no-op mode", exc_info=True)
        return False


def get_tracer(name: str = __name__):
    """Return a Tracer scoped to *name*."""
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name)
    return _get_noop_tracer(name)


def get_meter(name: str = __name__):
    """Return a Meter scoped to *name*."""
    if _meter_provider is not None:
        return _meter_provider.get_meter(name)
    return _get_noop_meter(name)


def shutdown() -> None:
    """Flush and shut down providers. Call on process exit."""
    global _tracer_provider, _meter_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_millis=5_000)
            _tracer_provider.shutdown()
        except Exception:
            logger.debug("Error shutting down TracerProvider", exc_info=True)
        _tracer_provider = None
    if _meter_provider is not None:
        try:
            _meter_provider.force_flush(timeout_millis=5_000)
            _meter_provider.shutdown()
        except Exception:
            logger.debug("Error shutting down MeterProvider", exc_info=True)
        _meter_provider = None


# ---------------------------------------------------------------------------
# Internal bootstrap (only runs when OTEL is enabled + SDK present)
# ---------------------------------------------------------------------------
def _do_init(service_name: str, service_version: str) -> bool:
    """Actual OTEL setup.  Allowed to raise — caller catches."""
    # Delay all OTEL imports to this function so the module loads cleanly
    # even when opentelemetry-sdk is not installed.
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    from core.telemetry_sanitizer import SensitiveAttributeScrubber

    global _tracer_provider, _meter_provider

    # ---- resource ----
    resource = Resource.create({
        SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", service_name),
        SERVICE_VERSION: service_version,
        "deployment.environment": os.getenv("ENVIRONMENT", "development"),
        "service.instance.id": f"{service_name}-{os.getpid()}",
    })

    # ---- tracing ----
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower() in ("true", "1")
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=insecure)

    _tracer_provider = TracerProvider(resource=resource)
    # Sanitizer runs BEFORE the batch exporter sees spans.
    _tracer_provider.add_span_processor(SensitiveAttributeScrubber())
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter,
            max_queue_size=2048,
            schedule_delay_millis=5_000,
            max_export_batch_size=512,
        )
    )
    trace.set_tracer_provider(_tracer_provider)

    # ---- metrics ----
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=60_000,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(_meter_provider)

    logger.info(
        "OTEL telemetry initialised: service=%s endpoint=%s pid=%d",
        service_name, endpoint, os.getpid(),
    )
    return True


# ---------------------------------------------------------------------------
# Lightweight fallback stubs when opentelemetry-api isn't installed
# (should be rare in practice, but keeps imports safe everywhere)
# ---------------------------------------------------------------------------
class _NoOpSpan:
    """Minimal span-like object that is always a no-op."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key, value):
        pass

    def set_status(self, status, description=None):
        pass

    def record_exception(self, exception, attributes=None):
        pass

    def add_event(self, name, attributes=None):
        pass

    def end(self):
        pass

    @property
    def is_recording(self):
        return False


class _FallbackNoOpTracer:
    """Returned when opentelemetry-api is not installed at all."""

    def start_span(self, name, **kwargs):
        return _NoOpSpan()

    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()


class _FallbackNoOpMeter:
    """Returned when opentelemetry-api is not installed at all."""

    def create_counter(self, name, **kw):
        return _NoOpInstrument()

    def create_up_down_counter(self, name, **kw):
        return _NoOpInstrument()

    def create_histogram(self, name, **kw):
        return _NoOpInstrument()

    def create_observable_gauge(self, name, callbacks=None, **kw):
        return _NoOpInstrument()


class _NoOpInstrument:
    def add(self, amount, attributes=None):
        pass

    def record(self, amount, attributes=None):
        pass
