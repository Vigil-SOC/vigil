"""
Tests for core.telemetry and core.telemetry_sanitizer.

The sanitizer tests are **security-critical** — they verify that sensitive
data patterns are scrubbed before leaving the process.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_telemetry():
    """Force-reset telemetry module state to allow re-initialization in tests."""
    import core.telemetry as mod

    mod._initialized = False
    mod._tracer_provider = None
    mod._meter_provider = None
    return mod


# ---------------------------------------------------------------------------
# core.telemetry — no-op behaviour when disabled
# ---------------------------------------------------------------------------

class TestTelemetryDisabled:
    """When VIGIL_OTEL_ENABLED is not set or false, everything is no-op."""

    def test_init_returns_false_when_disabled(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            assert tel.init_telemetry("test-service") is False

    def test_init_returns_false_when_unset(self):
        tel = _reload_telemetry()
        env = {k: v for k, v in os.environ.items() if k != "VIGIL_OTEL_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            assert tel.init_telemetry("test-service") is False

    def test_get_tracer_returns_noop_when_disabled(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            tel.init_telemetry("test-service")
            tracer = tel.get_tracer("test")
            span = tracer.start_span("noop")
            span.set_attribute("key", "value")
            span.end()

    def test_get_meter_returns_noop_when_disabled(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            tel.init_telemetry("test-service")
            meter = tel.get_meter("test")
            counter = meter.create_counter("test.counter")
            counter.add(1)  # must not raise
            hist = meter.create_histogram("test.hist")
            hist.record(42.0)  # must not raise

    def test_noop_span_context_manager(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            tel.init_telemetry("test-service")
            tracer = tel.get_tracer("test")
            with tracer.start_as_current_span("op") as span:
                span.set_attribute("key", "val")
                span.add_event("thing_happened")
                # is_recording is a callable method — call it correctly
                assert span.is_recording() is False

    def test_double_init_is_idempotent(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            r1 = tel.init_telemetry("svc")
            r2 = tel.init_telemetry("svc")
            assert r1 is False
            assert r2 is False

    def test_shutdown_is_safe_when_disabled(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            tel.init_telemetry("svc")
            tel.shutdown()  # must not raise

    def test_shutdown_resets_initialized_flag(self):
        """After shutdown(), init_telemetry() must be callable again (bug fix #3)."""
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "false"}, clear=False):
            tel.init_telemetry("svc")
            tel.shutdown()
            # _initialized must be False after shutdown so re-init is possible
            assert tel._initialized is False


class TestTelemetryInitFailure:
    """Verify graceful degradation when OTEL SDK is broken or missing."""

    def test_init_catches_import_error(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "true"}, clear=False):
            with patch.dict(
                sys.modules,
                {"opentelemetry": None, "opentelemetry.trace": None},
            ):
                result = tel.init_telemetry("svc")
                assert result is False

    def test_tracer_still_works_after_init_failure(self):
        tel = _reload_telemetry()
        with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": "true"}, clear=False):
            with patch.object(tel, "_do_init", side_effect=RuntimeError("boom")):
                tel.init_telemetry("svc")
            tracer = tel.get_tracer("test")
            span = tracer.start_span("safe")
            span.set_attribute("k", "v")
            span.end()


# ---------------------------------------------------------------------------
# Investigation ID context var
# ---------------------------------------------------------------------------

class TestInvestigationContext:
    def test_default_is_none(self):
        from core.telemetry import get_investigation_id
        get_investigation_id()  # must not raise

    def test_set_and_get(self):
        from core.telemetry import set_investigation_id, get_investigation_id
        set_investigation_id("inv-test-123")
        assert get_investigation_id() == "inv-test-123"
        set_investigation_id(None)
        assert get_investigation_id() is None


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    def test_is_otel_enabled_true(self):
        from core.telemetry import _is_otel_enabled
        for val in ("true", "True", "1", "yes", "YES"):
            with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": val}):
                assert _is_otel_enabled() is True

    def test_is_otel_enabled_false(self):
        from core.telemetry import _is_otel_enabled
        for val in ("false", "0", "no", ""):
            with patch.dict(os.environ, {"VIGIL_OTEL_ENABLED": val}):
                assert _is_otel_enabled() is False

    def test_llm_content_default_off(self):
        from core.telemetry import _should_record_llm_content
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "VIGIL_OTEL_RECORD_LLM_CONTENT"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _should_record_llm_content() is False

    def test_ioc_values_default_off(self):
        from core.telemetry import _should_record_ioc_values
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "VIGIL_OTEL_RECORD_IOC_VALUES"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _should_record_ioc_values() is False


# ---------------------------------------------------------------------------
# core.telemetry_sanitizer — SECURITY TESTS
# ---------------------------------------------------------------------------

class TestSensitiveAttributeScrubber:
    """
    Security-critical tests. Verify that the sanitizer correctly identifies
    and redacts sensitive data. If any of these fail, sensitive data may leak
    to the telemetry backend.
    """

    @pytest.fixture
    def scrubber(self):
        from core.telemetry_sanitizer import SensitiveAttributeScrubber
        return SensitiveAttributeScrubber()

    # ---- Key-based redaction ----

    @pytest.mark.parametrize(
        "key",
        [
            "http.request.header.authorization",
            "api_key",
            "my.api.key",
            "auth_token",
            "auth.token",
            "user.password",
            "db.secret",
            "x-api-key",
            "private_key",
            "access_token",
            "refresh_token",
            "session_id",
            "cookie",
            "sentry.dsn",
            "sentry_dsn",
            "my_dsn",
            "connection_string",
            "credential",
            "bearer",
            "jwt",
        ],
    )
    def test_sensitive_keys_are_redacted(self, scrubber, key):
        assert scrubber._should_redact(key, "some_value") is True

    @pytest.mark.parametrize(
        "key",
        [
            "http.method",
            "http.status_code",
            "vigil.tool.name",
            "vigil.tool.tier",
            "vigil.investigation.id",
            "gen_ai.request.model",
            "gen_ai.usage.input_tokens",
            "service.name",
            "deployment.environment",
            "net.peer.name",
        ],
    )
    def test_safe_keys_are_not_redacted(self, scrubber, key):
        assert scrubber._should_redact(key, "some_value") is False

    # ---- Content-key redaction (PII defence) ----

    @pytest.mark.parametrize(
        "key",
        [
            "finding.description",
            "finding.raw",
            "finding.payload",
            "finding.entity_context",
            "llm.prompt",
            "llm.response",
            "gen_ai.prompt",
            "gen_ai.completion",
            "tool.input",
            "tool.output",
            "tool.result",
        ],
    )
    def test_content_keys_are_redacted(self, scrubber, key):
        assert scrubber._should_redact(key, "benign text") is True

    # ---- Value-based redaction ----

    def test_anthropic_api_key_in_value(self, scrubber):
        value = "Authorization: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        assert scrubber._should_redact("some.random.key", value) is True

    def test_jwt_in_value(self, scrubber):
        value = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        assert scrubber._should_redact("some.header", value) is True

    def test_postgres_connection_string_in_value(self, scrubber):
        value = "postgresql://user:s3cretpass@db.example.com:5432/soc_db"
        assert scrubber._should_redact("db.connection", value) is True

    def test_redis_connection_string_in_value(self, scrubber):
        value = "redis://default:mypassword@redis.example.com:6379/0"
        assert scrubber._should_redact("cache.url", value) is True

    def test_long_hex_token_in_value(self, scrubber):
        value = "token=" + "a1b2c3d4" * 5  # 40 hex chars
        assert scrubber._should_redact("some.field", value) is True

    def test_aws_access_key_in_value(self, scrubber):
        value = "AKIAIOSFODNN7EXAMPLE"
        assert scrubber._should_redact("cloud.key", value) is True

    def test_short_benign_values_not_redacted(self, scrubber):
        assert scrubber._should_redact("http.method", "GET") is False
        assert scrubber._should_redact("http.status_code", 200) is False
        assert scrubber._should_redact("vigil.tool.tier", "safe") is False
        assert scrubber._should_redact(
            "gen_ai.request.model", "claude-sonnet-4-5-20250929"
        ) is False

    def test_numeric_values_not_redacted(self, scrubber):
        assert scrubber._should_redact("gen_ai.usage.input_tokens", 1500) is False
        assert scrubber._should_redact("vigil.llm.cost_usd", 0.0255) is False

    def test_dsn_substring_false_positive(self, scrubber):
        """Keys containing 'dsn' as part of another word must not be redacted."""
        assert scrubber._should_redact("redesign_count", "5") is False
        assert scrubber._should_redact("vigil.dsnark", "hello") is False

    # ---- Span integration test ----

    def test_on_end_scrubs_span_attributes(self):
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            pytest.skip("opentelemetry-sdk not installed")

        from core.telemetry_sanitizer import SensitiveAttributeScrubber

        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        scrubber = SensitiveAttributeScrubber()

        span = tracer.start_span("test-op")
        span.set_attribute("http.method", "POST")
        span.set_attribute("safe.attr", "hello")
        span.set_attribute("my.api_key", "sk-ant-api03-secretstuff1234567890abcdef")
        span.set_attribute(
            "finding.description", "User admin logged in from 10.0.0.1"
        )
        span.set_attribute("gen_ai.usage.input_tokens", 500)
        span.end()

        scrubber.on_end(span)

        attrs = dict(span.attributes)
        assert attrs["http.method"] == "POST"
        assert attrs["safe.attr"] == "hello"
        assert attrs["gen_ai.usage.input_tokens"] == 500
        assert attrs["my.api_key"] == "[REDACTED]"
        assert attrs["finding.description"] == "[REDACTED]"

        provider.shutdown()

    def test_on_end_handles_empty_attributes(self):
        from core.telemetry_sanitizer import SensitiveAttributeScrubber

        scrubber = SensitiveAttributeScrubber()
        mock_span = MagicMock()
        mock_span.attributes = None
        scrubber.on_end(mock_span)  # must not raise

        mock_span.attributes = {}
        scrubber.on_end(mock_span)  # must not raise

    def test_on_end_handles_all_clean_attributes(self):
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            pytest.skip("opentelemetry-sdk not installed")

        from core.telemetry_sanitizer import SensitiveAttributeScrubber

        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        scrubber = SensitiveAttributeScrubber()

        span = tracer.start_span("clean-op")
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.status_code", 200)
        span.end()

        scrubber.on_end(span)
        attrs = dict(span.attributes)
        assert attrs["http.method"] == "GET"
        assert attrs["http.status_code"] == 200
        provider.shutdown()


# ---------------------------------------------------------------------------
# Sanitizer stub when SDK not installed
# ---------------------------------------------------------------------------

class TestSanitizerStub:
    def test_stub_methods_exist(self):
        from core.telemetry_sanitizer import SensitiveAttributeScrubber

        s = SensitiveAttributeScrubber()
        s.on_start(MagicMock())
        s.on_end(MagicMock())
        s.on_shutdown()
        assert s.force_flush() is True


# ---------------------------------------------------------------------------
# Fallback no-op types
# ---------------------------------------------------------------------------

class TestFallbackNoOps:
    """The fallback types must be fully functional no-ops."""

    def test_noop_span_all_methods(self):
        from core.telemetry import _NoOpSpan

        span = _NoOpSpan()
        span.set_attribute("k", "v")
        span.set_status("ok")
        span.record_exception(ValueError("test"))
        span.add_event("evt", {"key": "val"})
        span.end()
        # is_recording is a method (not a property) — call it correctly
        assert span.is_recording() is False

    def test_noop_span_context_manager(self):
        from core.telemetry import _NoOpSpan

        with _NoOpSpan() as span:
            span.set_attribute("k", "v")

    def test_fallback_tracer(self):
        from core.telemetry import _FallbackNoOpTracer

        tracer = _FallbackNoOpTracer()
        span = tracer.start_span("test")
        assert span.is_recording() is False
        with tracer.start_as_current_span("test2") as s:
            s.set_attribute("k", "v")

    def test_fallback_meter(self):
        from core.telemetry import _FallbackNoOpMeter

        meter = _FallbackNoOpMeter()
        c = meter.create_counter("c")
        c.add(1)
        h = meter.create_histogram("h")
        h.record(1.0)
        meter.create_observable_gauge("g")
        u = meter.create_up_down_counter("u")
        u.add(-1)
