"""
Monitoring and error tracking configuration.
Integrates Sentry for error tracking and performance monitoring.
"""

import os
import time
import logging
from typing import Optional

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    """Initialize Sentry error tracking and performance monitoring."""

    sentry_dsn = os.getenv("SENTRY_DSN")
    environment = os.getenv("ENVIRONMENT", "development")
    release = os.getenv("RELEASE_VERSION", "unknown")

    if not sentry_dsn:
        logger.info("Sentry DSN not configured, skipping initialization")
        return

    sentry_sdk.init(
        dsn=sentry_dsn,
        environment=environment,
        release=release,
        # Performance Monitoring
        traces_sample_rate=0.1 if environment == "production" else 1.0,
        # Error tracking
        send_default_pii=False,  # Don't send PII
        attach_stacktrace=True,
        # Integrations
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR
            ),
        ],
        # Filtering
        before_send=before_send_filter,
        ignore_errors=[
            KeyboardInterrupt,
            "asyncio.CancelledError",
        ],
    )

    logger.info(f"Sentry initialized for environment: {environment}")


def before_send_filter(event, hint):
    """Filter events before sending to Sentry."""

    # Don't send health check errors
    if event.get("request", {}).get("url", "").endswith("/health"):
        return None

    # Don't send test errors
    if os.getenv("TESTING") == "true":
        return None

    return event


def capture_exception(error: Exception, context: Optional[dict] = None) -> None:
    """Manually capture an exception with additional context."""

    if context:
        sentry_sdk.set_context("custom", context)

    sentry_sdk.capture_exception(error)


def set_user_context(user_id: str, username: str, email: Optional[str] = None) -> None:
    """Set user context for error tracking."""

    sentry_sdk.set_user({
        "id": user_id,
        "username": username,
        "email": email,
    })


def add_breadcrumb(message: str, category: str = "default", level: str = "info", data: Optional[dict] = None) -> None:
    """Add a breadcrumb for debugging."""

    sentry_sdk.add_breadcrumb(
        message=message,
        category=category,
        level=level,
        data=data or {}
    )


# --- Prometheus metrics ---
# Defined at module level so they persist for the lifetime of the process.
# init_prometheus_metrics() previously defined these as locals (immediately GC'd).

PROMETHEUS_AVAILABLE = False

try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    http_requests_total = Counter(
        'http_requests_total',
        'Total HTTP requests',
        ['method', 'endpoint', 'status']
    )
    http_request_duration_seconds = Histogram(
        'http_request_duration_seconds',
        'HTTP request duration in seconds',
        ['method', 'endpoint']
    )
    active_cases_total = Gauge(
        'active_cases_total',
        'Number of active cases'
    )
    findings_processed_total = Counter(
        'findings_processed_total',
        'Total findings processed',
        ['source', 'severity']
    )

    PROMETHEUS_AVAILABLE = True

    class PrometheusMiddleware(BaseHTTPMiddleware):
        """Record request count and duration for every HTTP request."""

        async def dispatch(self, request: StarletteRequest, call_next):
            # Skip the /metrics endpoint itself to avoid noise
            if request.url.path == "/metrics":
                return await call_next(request)
            start = time.perf_counter()
            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                duration = time.perf_counter() - start
                http_requests_total.labels(
                    method=request.method,
                    endpoint=request.url.path,
                    status=status_code,
                ).inc()
                http_request_duration_seconds.labels(
                    method=request.method,
                    endpoint=request.url.path,
                ).observe(duration)

except ImportError:
    logger.warning("prometheus_client not installed, metrics disabled")


def get_metrics_response():
    """Return current Prometheus metrics as a FastAPI Response."""
    from fastapi.responses import Response

    if not PROMETHEUS_AVAILABLE:
        return Response("Prometheus not available", status_code=503)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
