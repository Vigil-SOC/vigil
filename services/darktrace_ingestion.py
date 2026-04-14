"""
Darktrace Ingestion Service.

Transforms inbound Darktrace webhook payloads (Model Breach, AI Analyst,
System Status) into Vigil finding dictionaries and hands them to the shared
``IngestionService``.

Darktrace pushes alerts via outbound webhooks (SaaS tenants have no syslog
option over the public internet), so this service does not poll. The
``fetch_alerts`` abstract method from ``SIEMIngestionService`` is implemented
as a no-op to satisfy the interface.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.siem_ingestion_service import SIEMIngestionService

logger = logging.getLogger(__name__)

DATA_SOURCE = "darktrace"
DEFAULT_EMBEDDING_DIM = 768


def _finding_id(prefix: str, stable_key: str, ts: datetime) -> str:
    """Generate a schema-compliant finding_id: f-YYYYMMDD-<8hex>.

    ``stable_key`` is hashed so the same Darktrace event always produces the
    same finding_id (idempotent replay through the webhook).
    """
    digest = hashlib.sha1(f"{prefix}:{stable_key}".encode("utf-8")).hexdigest()[:8]
    return f"f-{ts.strftime('%Y%m%d')}-{digest}"


def _parse_dt_time(value: Any) -> datetime:
    """Darktrace ships timestamps as epoch-ms ints or ISO strings."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        # Darktrace uses epoch milliseconds
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _score_to_anomaly(score: Any) -> float:
    """Darktrace breach scores are 0-1 floats. Clamp safely."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.5
    # Some older payloads ship 0-100 integers
    if s > 1.0:
        s = s / 100.0
    return max(0.0, min(1.0, s))


def _score_to_severity(score: float) -> str:
    if score >= 0.9:
        return "critical"
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    if score >= 0.2:
        return "low"
    return "info"


def _extract_mitre(tags_or_tactics: Any) -> Dict[str, float]:
    """AI Analyst payloads include MITRE tactic/technique tags.

    Darktrace emits either a list of dicts like ``{"name": "T1071.001"}`` or
    plain technique strings. We preserve only things that look like MITRE IDs
    and default confidence to 0.7 (Darktrace does not ship per-tag scores).
    """
    out: Dict[str, float] = {}
    if not tags_or_tactics:
        return out
    items = tags_or_tactics if isinstance(tags_or_tactics, list) else [tags_or_tactics]
    for item in items:
        if isinstance(item, dict):
            val = item.get("name") or item.get("id") or item.get("technique")
        else:
            val = item
        if not val:
            continue
        val = str(val).strip()
        # MITRE technique IDs start with T followed by digits
        if val.startswith("T") and val[1:].split(".")[0].isdigit():
            out[val] = 0.7
    return out


class DarktraceIngestionService(SIEMIngestionService):
    """Transform and ingest Darktrace webhook payloads."""

    def __init__(self, console_url: Optional[str] = None):
        super().__init__()
        self.siem_name = "Darktrace"
        self.console_url = (console_url or "").rstrip("/")

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Darktrace is push-only in this integration; no polling."""
        return []

    # Required by the abstract base. For webhook ingress we dispatch by type
    # via ``transform_model_breach`` / ``transform_ai_analyst`` /
    # ``transform_system_status`` — this default delegates by payload shape.
    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if "pbid" in alert or "model" in alert:
            return self.transform_model_breach(alert)
        if "uuid" in alert and ("title" in alert or "aianalyst" in str(alert).lower()):
            return self.transform_ai_analyst(alert)
        if "systemStatus" in alert or "status" in alert:
            return self.transform_system_status(alert)
        logger.warning("Unrecognized Darktrace payload shape; skipping")
        return None

    def transform_model_breach(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Transform a Darktrace Model Breach Alert."""
        pbid = alert.get("pbid")
        if pbid is None:
            logger.warning("Darktrace model breach missing pbid; skipping")
            return None

        ts = _parse_dt_time(alert.get("time") or alert.get("triggered"))
        score = _score_to_anomaly(alert.get("score"))
        device = alert.get("device") or {}
        model = alert.get("model") or {}

        entity_context: Dict[str, Any] = {}
        if device.get("ip"):
            entity_context["src_ip"] = device["ip"]
        if device.get("hostname"):
            entity_context["hostname"] = device["hostname"]
        if device.get("mac"):
            entity_context["mac"] = device["mac"]
        if alert.get("destinationIp"):
            entity_context["dst_ip"] = alert["destinationIp"]

        evidence_links = []
        if self.console_url:
            evidence_links.append(
                {
                    "type": "flow",
                    "ref": f"{self.console_url}/#modelbreach/{pbid}",
                }
            )

        return {
            "finding_id": _finding_id("dt-mb", str(pbid), ts),
            "embedding": [0.0] * DEFAULT_EMBEDDING_DIM,
            "mitre_predictions": _extract_mitre(model.get("tags")),
            "anomaly_score": score,
            "timestamp": ts.isoformat(),
            "data_source": DATA_SOURCE,
            "description": model.get("name") or "Darktrace Model Breach",
            "entity_context": entity_context or None,
            "evidence_links": evidence_links or None,
            "severity": _score_to_severity(score),
            "status": "new",
        }

    def transform_ai_analyst(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Transform a Darktrace AI Analyst Incident/Event."""
        uuid = alert.get("uuid") or alert.get("id")
        if not uuid:
            logger.warning("Darktrace AI Analyst payload missing uuid; skipping")
            return None

        ts = _parse_dt_time(
            alert.get("createdAt") or alert.get("start") or alert.get("time")
        )
        # AI Analyst exposes a 0-100 "groupScore" (event criticality)
        score = _score_to_anomaly(
            alert.get("groupScore") or alert.get("score") or 0.5
        )

        entity_context: Dict[str, Any] = {}
        breach_devices = alert.get("breachDevices") or alert.get("relatedBreaches") or []
        if breach_devices and isinstance(breach_devices, list):
            first = breach_devices[0] if isinstance(breach_devices[0], dict) else {}
            if first.get("ip"):
                entity_context["src_ip"] = first["ip"]
            if first.get("hostname"):
                entity_context["hostname"] = first["hostname"]

        evidence_links = []
        if self.console_url:
            evidence_links.append(
                {
                    "type": "flow",
                    "ref": f"{self.console_url}/#aianalyst/incident/{uuid}",
                }
            )

        return {
            "finding_id": _finding_id("dt-ai", str(uuid), ts),
            "embedding": [0.0] * DEFAULT_EMBEDDING_DIM,
            "mitre_predictions": _extract_mitre(
                alert.get("mitreTactics") or alert.get("tags")
            ),
            "anomaly_score": score,
            "timestamp": ts.isoformat(),
            "data_source": DATA_SOURCE,
            "description": alert.get("title") or "Darktrace AI Analyst Incident",
            "entity_context": entity_context or None,
            "evidence_links": evidence_links or None,
            "severity": _score_to_severity(score),
            "status": "new",
        }

    def transform_system_status(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Transform a Darktrace System Status Alert (health/operational)."""
        key = (
            alert.get("id")
            or alert.get("eventId")
            or alert.get("name")
            or str(hash(frozenset((k, str(v)) for k, v in alert.items())))
        )
        ts = _parse_dt_time(alert.get("time") or alert.get("timestamp"))
        # System status is informational/operational — keep score low
        score = 0.2
        status_level = str(alert.get("status") or alert.get("level") or "info").lower()
        severity = self.normalize_severity(status_level)

        return {
            "finding_id": _finding_id("dt-sys", str(key), ts),
            "embedding": [0.0] * DEFAULT_EMBEDDING_DIM,
            "mitre_predictions": {},
            "anomaly_score": score,
            "timestamp": ts.isoformat(),
            "data_source": DATA_SOURCE,
            "description": alert.get("message")
            or alert.get("name")
            or "Darktrace System Status",
            "entity_context": None,
            "severity": severity,
            "status": "new",
        }
