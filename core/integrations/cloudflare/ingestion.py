"""Cloudy / Cloudflare Security Insights ingestion.

Transforms a Cloudflare-pushed event (with an attached Cloudy natural-language
summary) into a Vigil finding. Because the public Cloudy webhook contract is
not stable as of writing, this transformer is intentionally permissive: any
fields the upstream payload happens to provide are forwarded into the finding
under `evidence.cloudy_summary` and `entity_context`.

Gated by `CLOUDY_INGESTION_ENABLED` — the webhook router refuses traffic when
the flag is off, and this service is only constructed by the router.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class CloudyIngestionService:
    """Build Vigil findings from Cloudflare Cloudy event payloads."""

    def __init__(self):
        # Lazy import: ingestion_service is also lazy elsewhere in the codebase;
        # this keeps Cloudy off the hot import path when the flag is off.
        from core.ingestion.ingestion_service import IngestionService

        self.ingestion_service = IngestionService()

    def transform_event(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Map a Cloudy webhook payload to a Vigil finding dict.

        Returns ``None`` if the payload is unparseable. Returns a dict suitable
        for `IngestionService.ingest_finding()` otherwise.
        """
        if not isinstance(payload, dict):
            return None

        event = payload.get("event") or payload
        cloudy_summary = (
            payload.get("cloudy_summary")
            or payload.get("summary")
            or event.get("cloudy_summary")
            or event.get("summary")
            or ""
        )
        if not cloudy_summary and not event:
            return None

        finding_id = str(payload.get("event_id") or payload.get("id") or uuid.uuid4())
        ip = (
            payload.get("client_ip")
            or event.get("client_ip")
            or event.get("source_ip")
        )
        host = payload.get("host") or event.get("host") or event.get("zone_name")
        action = (event.get("action") or event.get("ruleAction") or "").lower()

        severity = self._infer_severity(payload, event, cloudy_summary)
        mitre = self._extract_mitre(payload, event)

        entity_context: Dict[str, List[str] | str] = {}
        if ip:
            entity_context["src_ips"] = [ip]
        if host:
            entity_context["domains"] = [host]
        if event.get("user_email"):
            entity_context["usernames"] = [event["user_email"]]
        if event.get("user_agent"):
            entity_context["user_agents"] = [event["user_agent"]]

        evidence: Dict[str, Any] = {
            "cloudy_summary": cloudy_summary,
            "cloudy_provenance": {
                "event_id": payload.get("event_id"),
                "rule_id": event.get("rule_id"),
                "service": event.get("service"),
                "action": event.get("action"),
                "kind": event.get("kind") or payload.get("kind"),
            },
            "raw_payload": payload,
        }

        return {
            "finding_id": finding_id,
            "data_source": "cloudflare_cloudy",
            "timestamp": payload.get("timestamp") or event.get("timestamp") or _utcnow_iso(),
            "anomaly_score": float(payload.get("anomaly_score", 0.5)),
            "embedding": [0.0],  # ingestion service tolerates a placeholder
            "mitre_predictions": mitre,
            "severity": severity,
            "title": (
                payload.get("title")
                or event.get("title")
                or f"Cloudflare event ({action})"
                or "Cloudflare Cloudy alert"
            ),
            "description": cloudy_summary or event.get("description", ""),
            "entity_context": entity_context,
            "evidence": evidence,
        }

    @staticmethod
    def _infer_severity(
        payload: Dict[str, Any], event: Dict[str, Any], summary: str
    ) -> str:
        explicit = (
            payload.get("severity")
            or event.get("severity")
            or event.get("threat_level")
            or ""
        )
        if isinstance(explicit, str) and explicit.lower() in (
            "critical",
            "high",
            "medium",
            "low",
            "info",
        ):
            return explicit.lower()
        action = (event.get("action") or "").lower()
        if action in ("block", "challenge"):
            return "high"
        if "ransom" in summary.lower() or "exfil" in summary.lower():
            return "critical"
        return "medium"

    @staticmethod
    def _extract_mitre(payload: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
        mitre = payload.get("mitre_predictions") or event.get("mitre_predictions")
        if isinstance(mitre, dict):
            return mitre
        techniques = event.get("attack_techniques") or payload.get("attack_techniques")
        if isinstance(techniques, list):
            return {t: 1.0 for t in techniques if isinstance(t, str)}
        return {}
