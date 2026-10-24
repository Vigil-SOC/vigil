"""Cloudy / Cloudflare Security Insights ingestion.

Transforms a Cloudflare-pushed event (with an attached Cloudy natural-language
summary) into a Vigil finding. Because the public Cloudy webhook contract is
not stable as of writing, this transformer is intentionally permissive. The
summary becomes `description` and the raw payload `entity_context.source_evidence`.

Gated by `CLOUDY_INGESTION_ENABLED` — the webhook router refuses traffic when
the flag is off, and this service is only constructed by the router.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.ingestion.ingestion_service import IngestionService, _content_finding_id
from core.time import utcnow

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return utcnow().isoformat() + "Z"


class CloudyIngestionService:
    """Build Vigil findings from Cloudflare Cloudy event payloads."""

    def __init__(self):
        self.ingestion_service = IngestionService()

    def transform_event(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Map a Cloudy webhook payload to a Vigil finding dict.

        Returns ``None`` if the payload is unparseable. Returns a dict suitable
        for `IngestionService.ingest_finding()` otherwise.
        """
        event = payload.get("event") or payload
        if not isinstance(event, dict):
            return None
        cloudy_summary = (
            payload.get("cloudy_summary")
            or payload.get("summary")
            or event.get("cloudy_summary")
            or event.get("summary")
            or ""
        )
        action = event.get("action") or event.get("ruleAction") or ""
        title = payload.get("title") or event.get("title") or ""
        description = cloudy_summary or event.get("description", "")
        source_id = payload.get("event_id") or payload.get("id")
        external_id = str(source_id) if source_id else None
        if not all(
            isinstance(v, str) for v in (cloudy_summary, action, title, description)
        ):
            return None
        if external_id and len(external_id) > 255:
            return None
        if not cloudy_summary and not event:
            return None

        score = payload.get("anomaly_score")
        try:
            anomaly_score = float(0.5 if score is None else score)
        except (TypeError, ValueError):
            return None

        identity = external_id or json.dumps(payload, sort_keys=True)
        ip = (
            payload.get("client_ip") or event.get("client_ip") or event.get("source_ip")
        )
        host = payload.get("host") or event.get("host") or event.get("zone_name")

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

        return {
            "finding_id": _content_finding_id(f"cloudflare_cloudy:{identity}"),
            "external_id": external_id,
            "data_source": "cloudflare_cloudy",
            "timestamp": payload.get("timestamp")
            or event.get("timestamp")
            or _utcnow_iso(),
            "anomaly_score": anomaly_score,
            "mitre_predictions": mitre,
            "severity": severity,
            "title": title or f"Cloudflare event ({action.lower()})",
            "description": description,
            "entity_context": entity_context,
            "raw_data": payload,
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
    def _extract_mitre(
        payload: Dict[str, Any], event: Dict[str, Any]
    ) -> Dict[str, Any]:
        mitre = payload.get("mitre_predictions") or event.get("mitre_predictions")
        if isinstance(mitre, dict):
            return mitre
        techniques = event.get("attack_techniques") or payload.get("attack_techniques")
        if isinstance(techniques, list):
            return {t: 1.0 for t in techniques if isinstance(t, str)}
        return {}
