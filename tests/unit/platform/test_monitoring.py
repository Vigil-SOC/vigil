"""
Unit tests for core/platform/monitoring.py.

Stubs sentry_sdk so the tests run without a DSN or network (same pattern as
conftest.py for deeptempo_core).
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stub sentry_sdk before monitoring is imported
# ---------------------------------------------------------------------------
def _make_sentry_stub():
    sentry = types.ModuleType("sentry_sdk")
    sentry.init = MagicMock()
    sentry.capture_exception = MagicMock()
    sentry.set_user = MagicMock()
    sentry.set_context = MagicMock()
    sentry.add_breadcrumb = MagicMock()
    sys.modules["sentry_sdk"] = sentry

    for submod in [
        "sentry_sdk.integrations",
        "sentry_sdk.integrations.fastapi",
        "sentry_sdk.integrations.sqlalchemy",
        "sentry_sdk.integrations.logging",
    ]:
        mod = types.ModuleType(submod)
        sys.modules[submod] = mod

    sys.modules["sentry_sdk.integrations.fastapi"].FastApiIntegration = MagicMock()
    sys.modules["sentry_sdk.integrations.sqlalchemy"].SqlalchemyIntegration = MagicMock()
    sys.modules["sentry_sdk.integrations.logging"].LoggingIntegration = MagicMock()
    return sentry


_sentry_stub = _make_sentry_stub()

# Now import monitoring with the stub in place. It is a submodule, so a plain
# ``from core.platform import monitoring`` would hand back the attribute already
# cached on the parent package by whichever earlier test imported the app —
# with the real sentry bound. Drop it from sys.modules and go through
# importlib so module-level code re-runs against the stub above.
import importlib

sys.modules.pop("core.platform.monitoring", None)
monitoring = importlib.import_module("core.platform.monitoring")
_monitoring_module = monitoring


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitSentry(unittest.TestCase):
    def setUp(self):
        _sentry_stub.init.reset_mock()

    def test_no_op_when_dsn_missing(self):
        """init_sentry() should do nothing when SENTRY_DSN is not set."""
        env = {k: v for k, v in os.environ.items() if k != "SENTRY_DSN"}
        with patch.dict(os.environ, env, clear=True):
            monitoring.init_sentry()
        _sentry_stub.init.assert_not_called()

    def test_calls_sentry_init_when_dsn_present(self):
        """init_sentry() should call sentry_sdk.init when SENTRY_DSN is set."""
        with patch.dict(os.environ, {"SENTRY_DSN": "https://fake@sentry.io/123"}):
            monitoring.init_sentry()
        _sentry_stub.init.assert_called_once()
        kwargs = _sentry_stub.init.call_args[1]
        self.assertEqual(kwargs["dsn"], "https://fake@sentry.io/123")

    def test_production_sample_rate(self):
        """Production environment gets 0.1 traces_sample_rate."""
        with patch.dict(os.environ, {"SENTRY_DSN": "https://x@sentry.io/1", "ENVIRONMENT": "production"}):
            monitoring.init_sentry()
        kwargs = _sentry_stub.init.call_args[1]
        self.assertEqual(kwargs["traces_sample_rate"], 0.1)

    def test_dev_sample_rate(self):
        """Non-production environment gets 1.0 traces_sample_rate."""
        with patch.dict(os.environ, {"SENTRY_DSN": "https://x@sentry.io/1", "ENVIRONMENT": "development"}):
            monitoring.init_sentry()
        kwargs = _sentry_stub.init.call_args[1]
        self.assertEqual(kwargs["traces_sample_rate"], 1.0)

    def test_pii_not_sent(self):
        """send_default_pii must always be False."""
        with patch.dict(os.environ, {"SENTRY_DSN": "https://x@sentry.io/1"}):
            monitoring.init_sentry()
        kwargs = _sentry_stub.init.call_args[1]
        self.assertFalse(kwargs.get("send_default_pii"), "PII must never be sent to Sentry")


class TestBeforeSendFilter(unittest.TestCase):
    def test_allows_normal_events(self):
        event = {"request": {"url": "http://localhost/api/findings"}}
        with patch.dict(os.environ, {"TESTING": "false"}):
            result = monitoring.before_send_filter(event, {})
        self.assertIsNotNone(result)

    def test_drops_health_check(self):
        event = {"request": {"url": "http://localhost/health"}}
        result = monitoring.before_send_filter(event, {})
        self.assertIsNone(result)

    def test_drops_events_during_testing(self):
        event = {"request": {"url": "http://localhost/api/cases"}}
        with patch.dict(os.environ, {"TESTING": "true"}):
            result = monitoring.before_send_filter(event, {})
        self.assertIsNone(result)


class TestGetMetricsResponse(unittest.TestCase):
    def test_duplicate_http_instruments_are_gone(self):
        """The prometheus_client HTTP stack was deleted; OTEL FastAPI
        instrumentation is the only HTTP signal."""
        for name in (
            "PrometheusMiddleware",
            "PROMETHEUS_AVAILABLE",
            "http_requests_total",
            "http_request_duration_seconds",
            "active_cases_total",
            "findings_processed_total",
        ):
            self.assertFalse(hasattr(monitoring, name), name)

    def test_serves_default_registry(self):
        """get_metrics_response() renders prometheus_client's default REGISTRY —
        the one PrometheusMetricReader registers on — as Prometheus text."""
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider

        provider = MeterProvider(metric_readers=[PrometheusMetricReader()])
        try:
            provider.get_meter("t").create_counter("vigil_test_scrape").add(3)
            resp = monitoring.get_metrics_response()
        finally:
            provider.shutdown()  # unregisters the reader's collector

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.media_type)
        self.assertIn(b"# TYPE vigil_test_scrape_total counter", resp.body)
        self.assertRegex(resp.body, rb"vigil_test_scrape_total\{[^}]*\} 3\.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
