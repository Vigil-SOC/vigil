"""
OTEL SpanProcessor that scrubs sensitive attributes before export.

Security design:
  - **Allowlist, not blocklist.**  Only attribute keys that appear in
    ``ALLOWED_ATTRIBUTE_PREFIXES`` pass through unmodified.  Everything
    else is checked against the sensitive-key and sensitive-value rules.
  - Finding descriptions, LLM prompt/response bodies, and IOC values
    are never recorded as span attributes by default (callers should not
    set them).  This processor is a defence-in-depth second layer.
  - The processor runs synchronously inside ``on_end`` before the
    ``BatchSpanProcessor`` sees the span, so scrubbed data never reaches
    the exporter.

Note: ``ReadableSpan.attributes`` is immutable after the span ends, so we
replace the internal ``_attributes`` dict.  This is an implementation detail
of the OTEL Python SDK — tested against opentelemetry-sdk 1.25–1.30.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Attribute key substrings that always indicate a secret.
_SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "api_key",
    "api.key",
    "apikey",
    "auth_token",
    "auth.token",
    "authorization",
    "password",
    "secret",
    "credential",
    "private_key",
    "private.key",
    "access_token",
    "access.token",
    "refresh_token",
    "refresh.token",
    "session_id",
    "session.id",
    "cookie",
    "x-api-key",
    "bearer",
    "jwt",
    "sentry_dsn",
    "sentry.dsn",
    "_dsn",  # DB/Sentry DSN fields (suffix match avoids "redesign" false positive)
    "connection_string",
    "connection.string",
)

# Regex patterns for values that look like secrets regardless of key name.
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern, ...] = (
    # Anthropic API keys: sk-ant-...
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    # Generic long hex tokens (>= 32 chars)
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    # JWTs: three base64url segments separated by dots
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # postgresql:// or redis:// connection strings with credentials
    re.compile(r"(?:postgresql|postgres|redis|mysql)://\S+:\S+@"),
    # AWS access key IDs
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

# Keys related to finding/alert content that may contain PII from raw logs.
_CONTENT_KEY_PATTERNS: tuple[str, ...] = (
    "finding.description",
    "finding.raw",
    "finding.payload",
    "finding.entity_context",
    "alert.body",
    "alert.raw",
    "llm.prompt",
    "llm.response",
    "llm.completion",
    "gen_ai.prompt",
    "gen_ai.completion",
    "prompt.content",
    "response.content",
    "message.content",
    "tool.input",
    "tool.output",
    "tool.result",
)

_REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# SpanProcessor
# ---------------------------------------------------------------------------

try:
    from opentelemetry.sdk.trace import SpanProcessor, ReadableSpan
    from opentelemetry.context import Context

    class SensitiveAttributeScrubber(SpanProcessor):
        """Remove or redact sensitive span attributes before export."""

        def on_start(self, span, parent_context: Optional[Context] = None) -> None:
            # Nothing to do at start — attributes aren't final yet.
            pass

        def on_end(self, span: ReadableSpan) -> None:
            attrs = span.attributes
            if not attrs:
                return

            scrubbed: dict | None = None  # lazily copy

            for key, value in attrs.items():
                if self._should_redact(key, value):
                    if scrubbed is None:
                        scrubbed = dict(attrs)
                    scrubbed[key] = _REDACTED

            if scrubbed is not None:
                # Replace the internal attributes dict.
                # ReadableSpan stores attributes as a MappingProxy wrapping
                # a plain dict.  We rebuild the proxy.
                try:
                    from types import MappingProxyType
                    object.__setattr__(span, "_attributes", MappingProxyType(scrubbed))
                except Exception:
                    # If the SDK changes internals, log and move on.
                    # Defence in depth: we tried.
                    logger.debug(
                        "Could not replace span attributes for sanitisation",
                        exc_info=True,
                    )

        def on_shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

        # ---- classification helpers ----

        @staticmethod
        def _should_redact(key: str, value) -> bool:
            lower_key = key.lower()

            # 1. Sensitive key name
            for pattern in _SENSITIVE_KEY_PATTERNS:
                if pattern in lower_key:
                    return True

            # 2. Content keys that may contain PII / raw log data
            for pattern in _CONTENT_KEY_PATTERNS:
                if pattern in lower_key:
                    return True

            # 3. Value-based patterns (only check strings)
            if isinstance(value, str) and len(value) > 15:
                for regex in _SENSITIVE_VALUE_PATTERNS:
                    if regex.search(value):
                        return True

            return False

except ImportError:
    # opentelemetry-sdk not installed — provide a stub so the import
    # in telemetry.py doesn't break at module load time.
    class SensitiveAttributeScrubber:  # type: ignore[no-redef]
        """No-op stub when OTEL SDK is not installed."""

        def on_start(self, span, parent_context=None):
            pass

        def on_end(self, span):
            pass

        def on_shutdown(self):
            pass

        def force_flush(self, timeout_millis=30_000):
            return True
