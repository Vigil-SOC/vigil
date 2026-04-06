"""
Unit tests for backend/monitoring.py changes.

Stubs sentry_sdk and prometheus_client so the tests run without
those packages installed (same pattern as conftest.py for deeptempo_core).
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call


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


# ---------------------------------------------------------------------------
# Stub prometheus_client before monitoring is imported
# ---------------------------------------------------------------------------
def _make_prometheus_stub():
    prom = types.ModuleType("prometheus_client")
    prom.Counter = MagicMock(return_value=MagicMock())
    prom.Histogram = MagicMock(return_value=MagicMock())
    prom.Gauge = MagicMock(return_value=MagicMock())
    prom.generate_latest = MagicMock(return_value=b"# metrics\n")
    prom.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    sys.modules["prometheus_client"] = prom
    return prom


# Stub starlette so PrometheusMiddleware can be defined
def _make_starlette_stub():
    for name in ["starlette", "starlette.middleware", "starlette.middleware.base", "starlette.requests"]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    class _BaseHTTPMiddleware:
        def __init__(self, app):
            self.app = app

    sys.modules["starlette.middleware.base"].BaseHTTPMiddleware = _BaseHTTPMiddleware
    sys.modules["starlette.requests"].Request = object


_sentry_stub = _make_sentry_stub()
_prometheus_stub = _make_prometheus_stub()
_make_starlette_stub()

# Now import monitoring with stubs in place
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
import importlib
import monitoring as _monitoring_module

# Force a fresh import so module-level code runs with our stubs
if "monitoring" in sys.modules:
    del sys.modules["monitoring"]
import monitoring


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


class TestPrometheusAvailable(unittest.TestCase):
    def test_prometheus_available_flag_is_true(self):
        """prometheus_client is stubbed in, so PROMETHEUS_AVAILABLE should be True."""
        self.assertTrue(monitoring.PROMETHEUS_AVAILABLE)

    def test_module_level_metrics_exist(self):
        """All four metrics should be defined at module level."""
        self.assertTrue(hasattr(monitoring, "http_requests_total"))
        self.assertTrue(hasattr(monitoring, "http_request_duration_seconds"))
        self.assertTrue(hasattr(monitoring, "active_cases_total"))
        self.assertTrue(hasattr(monitoring, "findings_processed_total"))

    def test_prometheus_middleware_class_exists(self):
        """PrometheusMiddleware class should be importable from monitoring."""
        self.assertTrue(hasattr(monitoring, "PrometheusMiddleware"))


class TestGetMetricsResponse(unittest.TestCase):
    def test_returns_prometheus_output(self):
        """get_metrics_response() should return a Response with Prometheus content."""
        _prometheus_stub.generate_latest.return_value = b"# HELP http_requests_total\n"

        class _FakeResponse:
            def __init__(self, content, media_type=None):
                self.content = content
                self.media_type = media_type

        fastapi_mod = types.ModuleType("fastapi")
        fastapi_responses_mod = types.ModuleType("fastapi.responses")
        fastapi_responses_mod.Response = _FakeResponse
        fastapi_mod.responses = fastapi_responses_mod

        with patch.dict(sys.modules, {"fastapi": fastapi_mod, "fastapi.responses": fastapi_responses_mod}):
            resp = monitoring.get_metrics_response()

        self.assertEqual(resp.content, b"# HELP http_requests_total\n")
        self.assertIn("text/plain", resp.media_type)


class TestPrometheusMiddlewareSkipsMetricsEndpoint(unittest.TestCase):
    """PrometheusMiddleware should not record a metric for /metrics itself."""

    def test_metrics_path_bypasses_recording(self):
        """Requests to /metrics should call call_next without recording."""
        import asyncio

        middleware = monitoring.PrometheusMiddleware(app=MagicMock())

        fake_response = MagicMock()
        fake_response.status_code = 200

        async def fake_call_next(req):
            return fake_response

        class FakeURL:
            path = "/metrics"

        class FakeRequest:
            url = FakeURL()
            method = "GET"

        result = asyncio.get_event_loop().run_until_complete(
            middleware.dispatch(FakeRequest(), fake_call_next)
        )
        # Should return the response unchanged without touching metrics
        self.assertEqual(result, fake_response)
        # No labels() call means no metric was recorded
        monitoring.http_requests_total.labels.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
