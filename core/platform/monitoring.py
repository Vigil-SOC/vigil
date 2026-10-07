"""
Monitoring and error tracking configuration.
Integrates Sentry for error tracking and performance monitoring.

When OTEL is active (VIGIL_OTEL_ENABLED=true), Sentry's own distributed
tracing is disabled (traces_sample_rate=0) to prevent double-tracing.
The SentrySpanProcessor bridges OTEL error spans to Sentry breadcrumbs so
both systems remain useful without creating duplicate transaction records.
"""

import logging
from typing import Any, Optional

import sentry_sdk
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from core.backup.status import read_last_success_at
from core.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OTEL ↔ Sentry bridge
# ---------------------------------------------------------------------------

try:
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.trace import StatusCode

    class SentrySpanProcessor(SpanProcessor):
        """
        Forwards OTEL ERROR spans to Sentry as breadcrumbs and attaches the
        OTEL trace_id to the Sentry scope for cross-system correlation.

        Registered in core/telemetry.py when SENTRY_DSN is present.
        Does NOT create Sentry transactions — Sentry tracing is disabled
        when OTEL is active to prevent double-tracing.
        """

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            try:
                ctx = span.get_span_context()
                if ctx and ctx.is_valid:
                    sentry_sdk.set_tag("otel.trace_id", format(ctx.trace_id, "032x"))
            except Exception:
                pass

        def on_end(self, span: Any) -> None:
            try:
                if span.status.status_code != StatusCode.ERROR:
                    return
                sentry_sdk.add_breadcrumb(
                    message=span.name,
                    category="otel.span",
                    level="error",
                    data={
                        "span_id": format(span.get_span_context().span_id, "016x"),
                    },
                )
            except Exception:
                pass

        def on_shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

except ImportError:
    # OTEL SDK not installed — provide a stub so the import never fails
    class SentrySpanProcessor:  # type: ignore[no-redef]
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            pass

        def on_end(self, span: Any) -> None:
            pass

        def on_shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True


# ---------------------------------------------------------------------------
# Sentry initialization
# ---------------------------------------------------------------------------


def init_sentry() -> None:
    """Initialize Sentry error tracking and performance monitoring."""

    settings = get_settings()
    sentry_dsn = settings.sentry_dsn
    environment = settings.environment
    release = settings.release_version

    if not sentry_dsn:
        logger.info("Sentry DSN not configured, skipping initialization")
        return

    # When OTEL is active, disable Sentry's own distributed tracing to prevent
    # double-tracing. Sentry still captures errors — tracing is OTEL's job.
    otel_active = get_settings().vigil_otel_enabled
    traces_sample_rate = (
        0.0 if otel_active else (0.1 if environment == "production" else 1.0)
    )

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=environment,
        release=release,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        attach_stacktrace=True,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
        before_send=before_send_filter,
        ignore_errors=[
            KeyboardInterrupt,
            "asyncio.CancelledError",
        ],
    )

    logger.info(
        "Sentry initialized for environment: %s (OTEL tracing: %s)",
        environment,
        "disabled" if otel_active else "enabled",
    )


def before_send_filter(event, hint):
    """Filter events before sending to Sentry."""

    # Don't send health check errors
    if event.get("request", {}).get("url", "").endswith("/health"):
        return None

    # Don't send test errors
    if get_settings().testing:
        return None

    return event


def capture_exception(error: Exception, context: Optional[dict] = None) -> None:
    """Manually capture an exception with additional context."""

    if context:
        sentry_sdk.set_context("custom", context)

    sentry_sdk.capture_exception(error)


def add_breadcrumb(
    message: str,
    category: str = "default",
    level: str = "info",
    data: Optional[dict] = None,
) -> None:
    """Add a breadcrumb for debugging."""

    sentry_sdk.add_breadcrumb(
        message=message, category=category, level=level, data=data or {}
    )


def get_metrics_response() -> Response:
    """Prometheus text for the process.

    The OTEL PrometheusMetricReader (core/telemetry.py) registers its collector
    on prometheus_client's default REGISTRY, so this is the OTEL MeterProvider
    rendered as Prometheus text. HTTP request metrics come from
    FastAPIInstrumentation, not from anything declared here.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# A Gauge constructed at import always emits a number, including 0 when no
# backup has succeeded. This collector yields the family only when the status
# file has a parseable timestamp, so a missing file exports no sample.
_BACKUP_METRIC = "vigil_backup_last_success_timestamp_seconds"
_BACKUP_METRIC_HELP = "Unix time of the last successful backup."


class _BackupLastSuccessCollector:
    def describe(self):
        yield GaugeMetricFamily(_BACKUP_METRIC, _BACKUP_METRIC_HELP)

    def collect(self):
        when = read_last_success_at()
        if when is None:
            return
        yield GaugeMetricFamily(
            _BACKUP_METRIC, _BACKUP_METRIC_HELP, value=when.timestamp()
        )


def _register_backup_collector() -> None:
    # tests/unit/platform/test_monitoring.py reimports this module. The
    # previous collector still owns the series name on the default registry.
    previous = REGISTRY._names_to_collectors.get(_BACKUP_METRIC)
    if previous is not None:
        REGISTRY.unregister(previous)
    REGISTRY.register(_BackupLastSuccessCollector())


_register_backup_collector()
