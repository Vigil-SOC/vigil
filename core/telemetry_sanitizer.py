"""
core/telemetry_sanitizer.py — Blocklist-based span processor for Vigil SOC.

Scrubs sensitive attributes from OTEL spans before they reach any exporter.
Runs synchronously in on_end() so redacted data never leaves the process.

Design: BLOCKLIST — attributes whose keys match known-sensitive patterns, or
whose values contain recognisable secrets, are replaced with "[REDACTED]".
Unrecognised attributes pass through. This is defence-in-depth; rely on
careful attribute naming conventions to prevent accidental leakage.

Note: LLM content (prompts/responses) and raw finding/IOC values are always
redacted unless the operator has explicitly opted in via environment variables
VIGIL_OTEL_RECORD_LLM_CONTENT and VIGIL_OTEL_RECORD_IOC_VALUES respectively.
"""
from __future__ import annotations

import re
import logging
from types import MappingProxyType
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from opentelemetry.sdk.trace import SpanProcessor
    _SDK_AVAILABLE = True
except ImportError:
    SpanProcessor = object  # type: ignore[assignment,misc]
    _SDK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Blocklist patterns
# ---------------------------------------------------------------------------

# Key substrings that indicate a sensitive attribute (matched as whole segments)
_SENSITIVE_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "password",
        "passwd",
        "secret",
        "private_key",
        "access_token",
        "refresh_token",
        "session_id",
        "cookie",
        "dsn",
        "connection_string",
        "credential",
        "bearer",
        "jwt",
        "x-api-key",
    }
)

# Key substrings that indicate the value contains PII or raw security content
_CONTENT_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
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
    }
)

# Value-based regexes that detect secret material regardless of key name
_SECRET_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-", re.IGNORECASE),  # Anthropic API key prefix
    re.compile(
        r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"
    ),  # JWT (base64url.base64url.base64url)
    re.compile(
        r"(postgresql|postgres|redis|mysql|mongodb)://[^\s]+:[^\s]+@",
        re.IGNORECASE,
    ),  # Database / cache connection strings with credentials
    re.compile(
        r"(?<![a-fA-F0-9])[a-fA-F0-9]{32,}(?![a-fA-F0-9])"
    ),  # Long hex tokens (API keys, hashes used as secrets)
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ID
]

# Separator characters used when splitting key segments
_KEY_SEP_RE = re.compile(r"[._\-]")


class SensitiveAttributeScrubber(SpanProcessor):  # type: ignore[misc]
    """
    Blocklist-based SpanProcessor that redacts sensitive span attributes.

    Registered *before* the BatchSpanProcessor so scrubbing happens before
    any data leaves the process boundary.
    """

    # ------------------------------------------------------------------
    # Redaction logic (public for unit testing)
    # ------------------------------------------------------------------

    def _key_segment_matches(self, key_lower: str, substr: str) -> bool:
        """Return True if *substr* appears as a contiguous run of whole segments
        within *key_lower* (segments are split on '.', '_', '-').

        Multi-word patterns (e.g. "api_key") are split into ["api","key"] and
        matched as a contiguous sub-sequence, so:
          - "my.api.key"   matches "api_key"  ✓
          - "x-api-key"    matches "api_key"  ✓
          - "vigil.dsnark" does NOT match "dsn"  ✓  (false-positive prevention)
          - "redesign_count" does NOT match "dsn" ✓
        """
        key_segs = _KEY_SEP_RE.split(key_lower)
        sub_segs = _KEY_SEP_RE.split(substr)
        n, m = len(key_segs), len(sub_segs)
        if m > n:
            return False
        for i in range(n - m + 1):
            if key_segs[i : i + m] == sub_segs:
                return True
        return False

    def _should_redact(self, key: str, value: Any) -> bool:
        """Return True if this attribute should be replaced with [REDACTED]."""
        key_lower = key.lower()

        # 1. Key-based: check for whole-segment (contiguous) matches
        for substr in _SENSITIVE_KEY_SUBSTRINGS:
            if self._key_segment_matches(key_lower, substr):
                return True

        # 2. Key-based: content fields that may carry PII / raw security data
        for content_key in _CONTENT_KEY_SUBSTRINGS:
            if content_key in key_lower:
                return True

        # 3. Value-based: detect recognisable secrets in string values
        if isinstance(value, str) and len(value) > 6:
            for pattern in _SECRET_VALUE_PATTERNS:
                if pattern.search(value):
                    return True

        return False

    # ------------------------------------------------------------------
    # SpanProcessor interface
    # ------------------------------------------------------------------

    def on_start(self, span: Any, parent_context: Optional[Any] = None) -> None:
        pass  # Nothing to do before span ends

    def on_end(self, span: Any) -> None:
        if not _SDK_AVAILABLE:
            return

        try:
            attrs = span.attributes
            if not attrs:
                return

            # Check operator opt-in flags for LLM content and IOC values
            from core.telemetry import _should_record_llm_content, _should_record_ioc_values

            record_llm = _should_record_llm_content()
            record_ioc = _should_record_ioc_values()

            new_attrs: dict[str, Any] = {}
            modified = False

            for key, value in attrs.items():
                key_lower = key.lower()

                # LLM prompt/response: redact unless operator opted in
                if not record_llm and any(
                    k in key_lower
                    for k in (
                        "llm.prompt",
                        "llm.response",
                        "gen_ai.prompt",
                        "gen_ai.completion",
                    )
                ):
                    new_attrs[key] = "[REDACTED]"
                    modified = True
                    continue

                # Raw finding / IOC values: redact unless operator opted in
                if not record_ioc and "finding." in key_lower and any(
                    k in key_lower for k in ("raw", "payload", "description", "entity")
                ):
                    new_attrs[key] = "[REDACTED]"
                    modified = True
                    continue

                # General blocklist check
                if self._should_redact(key, value):
                    new_attrs[key] = "[REDACTED]"
                    modified = True
                else:
                    new_attrs[key] = value

            if modified:
                # ReadableSpan._attributes is immutable after end().
                # We patch the internal dict and rewrap with MappingProxyType.
                span._attributes = MappingProxyType(new_attrs)

        except Exception as exc:
            logger.debug("Sanitizer on_end failed (non-fatal): %s", exc)

    def on_shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
