"""
core/telemetry_config.py — shared telemetry configuration helpers.

Extracted so both ``core.telemetry`` and ``core.telemetry_sanitizer`` can
read operator opt-in flags without importing each other (avoids an import
cycle between the bootstrap and the span-scrubbing processor).
"""
from __future__ import annotations

import os


def _should_record_llm_content() -> bool:
    """Return True only when the operator has explicitly opted in."""
    val = os.environ.get("VIGIL_OTEL_RECORD_LLM_CONTENT", "").lower()
    return val in ("true", "1", "yes")


def _should_record_ioc_values() -> bool:
    """Return True only when the operator has explicitly opted in."""
    val = os.environ.get("VIGIL_OTEL_RECORD_IOC_VALUES", "").lower()
    return val in ("true", "1", "yes")
