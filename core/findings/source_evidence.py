"""Normalize bounded source evidence attached to security findings.

The finding schema intentionally keeps source evidence inside ``entity_context``
so existing databases can adopt the contract without a migration.  The
envelope is source-agnostic; ``telemetry_kind`` selects the presentation while
``schema_id`` identifies the producer-owned record shape.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Dict, Optional


SOURCE_EVIDENCE_VERSION = 1
SOURCE_EVIDENCE_PREVIEW_LIMIT = 100
SOURCE_EVIDENCE_RAW_TEXT_LIMIT = 65_536

TELEMETRY_KINDS = {"netflow", "dns", "http_session", "generic_log"}
SOURCE_EVIDENCE_STATUSES = {
    "available",
    "not_in_artifact",
    "redacted",
    "invalid",
}
SOURCE_EVIDENCE_PROVENANCE = {"embedded", "joined"}

_KIND_ALIASES = {
    "net": "netflow",
    "network": "netflow",
    "network_flow": "netflow",
    "dns_flow": "dns",
    "http": "http_session",
    "session": "http_session",
    "web_session": "http_session",
    "event": "generic_log",
    "events": "generic_log",
    "log": "generic_log",
    "logs": "generic_log",
}

_DEFAULT_SCHEMA_IDS = {
    "netflow": "netflow.v1",
    "dns": "dns.v1",
    "http_session": "http-session.v1",
    "generic_log": "generic-log.v1",
}


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _telemetry_kind(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    normalized = _KIND_ALIASES.get(normalized, normalized)
    return normalized if normalized in TELEMETRY_KINDS else None


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Convert common Parquet values into JSON-safe values.

    Evidence remains source-ordered.  The depth and collection limits prevent
    a malformed source field from expanding without bound inside JSONB.
    """
    if depth >= 8:
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:100]]
    return str(value)


def _invalid_evidence(
    kind: Optional[str] = None, schema_id: Any = None
) -> Dict[str, Any]:
    resolved_kind = kind or "generic_log"
    return {
        "version": SOURCE_EVIDENCE_VERSION,
        "telemetry_kind": resolved_kind,
        "schema_id": _text(schema_id) or _DEFAULT_SCHEMA_IDS[resolved_kind],
        "status": "invalid",
        "provenance": "embedded",
    }


def normalize_source_evidence(
    value: Any,
    *,
    default_kind: Any = None,
    default_schema_id: Any = None,
) -> Dict[str, Any]:
    """Return a validated, bounded v1 source-evidence envelope.

    Malformed explicit evidence becomes a truthful ``invalid`` state instead
    of being passed through or causing the entire finding to be dropped.
    """
    fallback_kind = _telemetry_kind(default_kind)
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return _invalid_evidence(fallback_kind, default_schema_id)
    if not isinstance(parsed, Mapping):
        return _invalid_evidence(fallback_kind, default_schema_id)

    version = parsed.get("version", SOURCE_EVIDENCE_VERSION)
    kind = _telemetry_kind(parsed.get("telemetry_kind")) or fallback_kind
    schema_id = _text(parsed.get("schema_id")) or _text(default_schema_id)
    status = _text(parsed.get("status"))
    provenance = _text(parsed.get("provenance")) or "embedded"

    if (
        type(version) is not int
        or version != SOURCE_EVIDENCE_VERSION
        or kind is None
        or status not in SOURCE_EVIDENCE_STATUSES
        or provenance not in SOURCE_EVIDENCE_PROVENANCE
    ):
        return _invalid_evidence(kind or fallback_kind, schema_id)

    envelope: Dict[str, Any] = {
        "version": SOURCE_EVIDENCE_VERSION,
        "telemetry_kind": kind,
        "schema_id": schema_id or _DEFAULT_SCHEMA_IDS[kind],
        "status": status,
        "provenance": provenance,
    }
    if status != "available":
        return envelope

    raw_records = parsed.get("records")
    if raw_records is None:
        records = []
    elif isinstance(raw_records, (list, tuple)):
        records = [
            _json_safe(record if isinstance(record, Mapping) else {"value": record})
            for record in raw_records[:SOURCE_EVIDENCE_PREVIEW_LIMIT]
        ]
    else:
        return _invalid_evidence(kind, envelope["schema_id"])

    raw_text = parsed.get("raw_text")
    if raw_text is None:
        cleaned_raw_text = None
        raw_text_truncated = False
    elif isinstance(raw_text, str):
        cleaned_raw_text = raw_text[:SOURCE_EVIDENCE_RAW_TEXT_LIMIT]
        raw_text_truncated = len(raw_text) > SOURCE_EVIDENCE_RAW_TEXT_LIMIT
    else:
        return _invalid_evidence(kind, envelope["schema_id"])

    if not records and not cleaned_raw_text:
        return _invalid_evidence(kind, envelope["schema_id"])

    supplied_total = parsed.get("total_records")
    if supplied_total is None:
        total_records = len(raw_records or [])
    elif isinstance(supplied_total, bool) or (
        isinstance(supplied_total, float) and not supplied_total.is_integer()
    ):
        return _invalid_evidence(kind, envelope["schema_id"])
    else:
        try:
            total_records = int(supplied_total)
        except (TypeError, ValueError):
            return _invalid_evidence(kind, envelope["schema_id"])
    if total_records < len(records):
        return _invalid_evidence(kind, envelope["schema_id"])

    supplied_truncated = parsed.get("truncated", False)
    supplied_raw_text_truncated = parsed.get("raw_text_truncated", False)
    if not isinstance(supplied_truncated, bool) or not isinstance(
        supplied_raw_text_truncated, bool
    ):
        return _invalid_evidence(kind, envelope["schema_id"])

    envelope["total_records"] = total_records
    envelope["truncated"] = bool(
        supplied_truncated
        or total_records > len(records)
        or (raw_records is not None and len(raw_records) > len(records))
    )
    if records:
        envelope["records"] = records
    if cleaned_raw_text:
        envelope["raw_text"] = cleaned_raw_text
        envelope["raw_text_truncated"] = bool(
            supplied_raw_text_truncated or raw_text_truncated
        )
    return envelope


def source_evidence_from_loglm_row(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Build source evidence from optional LogLM Parquet columns.

    Preferred input is the canonical ``source_evidence`` object/JSON column.
    For current LogLM artifacts, ``events_json`` and ``sequence`` are adapted
    into the same envelope.  ``source_evidence_kind`` is the explicit renderer
    selector; without it, embedded events use the generic-log renderer.
    """
    kind_value = row.get("source_evidence_kind")
    kind = _telemetry_kind(kind_value)
    schema_id = row.get("source_evidence_schema_id")
    if kind_value is not None and kind is None:
        return _invalid_evidence(schema_id=schema_id)
    explicit = row.get("source_evidence")
    if explicit is not None:
        return normalize_source_evidence(
            explicit,
            default_kind=kind,
            default_schema_id=schema_id,
        )

    status = _text(row.get("source_evidence_status"))
    if status is not None and status not in SOURCE_EVIDENCE_STATUSES:
        return _invalid_evidence(kind, schema_id)
    events_value = row.get("events_json")
    sequence_value = row.get("sequence")
    raw_text = (
        sequence_value
        if isinstance(sequence_value, str) and sequence_value.strip()
        else None
    )

    has_evidence_columns = any(
        value is not None
        for value in (kind_value, schema_id, status, events_value, sequence_value)
    )
    if not has_evidence_columns:
        return None

    resolved_kind = kind or "generic_log"
    if status in {"not_in_artifact", "redacted", "invalid"}:
        return normalize_source_evidence(
            {
                "version": SOURCE_EVIDENCE_VERSION,
                "telemetry_kind": resolved_kind,
                "schema_id": _text(schema_id) or _DEFAULT_SCHEMA_IDS[resolved_kind],
                "status": status,
                "provenance": (
                    _text(row.get("source_evidence_provenance")) or "embedded"
                ),
            }
        )

    records = None
    if events_value is not None:
        if isinstance(events_value, str):
            try:
                records = json.loads(events_value)
            except (TypeError, ValueError):
                if raw_text is None:
                    return _invalid_evidence(resolved_kind, schema_id)
                records = None
        else:
            records = events_value
        if records is not None and not isinstance(records, (list, tuple)):
            if raw_text is None:
                return _invalid_evidence(resolved_kind, schema_id)
            records = None

    if records is None and raw_text is None:
        missing_status = (
            status if status in SOURCE_EVIDENCE_STATUSES else "not_in_artifact"
        )
        return normalize_source_evidence(
            {
                "version": SOURCE_EVIDENCE_VERSION,
                "telemetry_kind": resolved_kind,
                "schema_id": _text(schema_id) or _DEFAULT_SCHEMA_IDS[resolved_kind],
                "status": missing_status,
                "provenance": (
                    _text(row.get("source_evidence_provenance")) or "embedded"
                ),
            }
        )

    total_records = row.get("source_evidence_total_records")
    if total_records is None and isinstance(records, (list, tuple)):
        total_records = len(records)
    return normalize_source_evidence(
        {
            "version": SOURCE_EVIDENCE_VERSION,
            "telemetry_kind": resolved_kind,
            "schema_id": _text(schema_id) or _DEFAULT_SCHEMA_IDS[resolved_kind],
            "status": "available",
            "provenance": _text(row.get("source_evidence_provenance")) or "embedded",
            "total_records": total_records,
            "records": records,
            "raw_text": raw_text,
        }
    )


def normalize_finding_source_evidence(finding: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an envelope already attached to a finding without mutation."""
    context = finding.get("entity_context")
    if not isinstance(context, Mapping):
        return finding
    evidence = context.get("source_evidence")
    if evidence is None:
        return finding

    normalized = dict(finding)
    normalized_context = dict(context)
    normalized_context["source_evidence"] = normalize_source_evidence(evidence)
    normalized["entity_context"] = normalized_context
    return normalized


def project_finding_source_evidence_for_list(finding: Dict[str, Any]) -> Dict[str, Any]:
    """Strip bulky evidence payloads from a finding list item without mutation."""
    normalized = normalize_finding_source_evidence(finding)
    context = normalized.get("entity_context")
    if not isinstance(context, Mapping):
        return normalized
    evidence = context.get("source_evidence")
    if not isinstance(evidence, Mapping):
        return normalized

    projected = dict(normalized)
    projected_context = dict(context)
    projected_evidence = dict(evidence)
    projected_evidence.pop("records", None)
    projected_evidence.pop("raw_text", None)
    projected_evidence["payload_included"] = False
    projected_context["source_evidence"] = projected_evidence
    projected["entity_context"] = projected_context
    return projected
