"""
core/telemetry_config.py — shared telemetry configuration helpers.

Extracted so both ``core.telemetry`` and ``core.telemetry_sanitizer`` can
read operator opt-in flags without importing each other (avoids an import
cycle between the bootstrap and the span-scrubbing processor).
"""

from __future__ import annotations

from core.config import get_settings


def _should_record_llm_content() -> bool:
    return get_settings().vigil_otel_record_llm_content


def _should_record_ioc_values() -> bool:
    return get_settings().vigil_otel_record_ioc_values
