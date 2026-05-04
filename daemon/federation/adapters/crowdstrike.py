"""CrowdStrike Falcon federation adapter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from core.config import get_integration_config, is_integration_enabled
from daemon.federation.adapters._base import fresh_cursor, parse_cursor_since
from daemon.federation.registry import (
    FederationAdapter,
    FetchResult,
    register_adapter,
)

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "Critical": "critical",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "Informational": "low",
}


class CrowdStrikeAdapter:
    name = "crowdstrike"

    def __init__(self) -> None:
        self._service = None

    def is_configured(self) -> bool:
        return is_integration_enabled("crowdstrike")

    def default_interval(self) -> int:
        return 60  # EDR cadence — sub-minute matters here

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.is_configured():
            return None
        try:
            from services.crowdstrike_service import CrowdStrikeService

            cfg = get_integration_config("crowdstrike")
            self._service = CrowdStrikeService(
                client_id=cfg.get("client_id", ""),
                client_secret=cfg.get("client_secret", ""),
                base_url=cfg.get("base_url", "https://api.crowdstrike.com"),
            )
        except Exception as e:
            logger.warning("CrowdStrike service init failed: %s", e)
            self._service = None
        return self._service

    async def fetch(
        self,
        *,
        since: Optional[datetime],
        cursor: Dict[str, Any],
        max_items: int,
    ) -> FetchResult:
        svc = self._get_service()
        if svc is None:
            return FetchResult(findings=[], cursor=fresh_cursor())

        cutoff = parse_cursor_since(cursor) or since
        if cutoff is None:
            # First run: small window, no backfill.
            cutoff = datetime.utcnow() - timedelta(minutes=1)

        try:
            detections = svc.get_detections(
                filter_query=f"created_timestamp:>='{cutoff.isoformat()}Z'",
                limit=max_items,
            ) or []
        except Exception as e:
            logger.debug("CrowdStrike fetch failed: %s", e)
            detections = []

        findings = []
        for det in detections[:max_items]:
            f = _detection_to_finding(det)
            if f is not None:
                findings.append(f)

        return FetchResult(findings=findings, cursor=fresh_cursor())


def _detection_to_finding(detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    detection_id = detection.get("detection_id", "")
    if not detection_id:
        return None

    external_id = str(detection_id)[:128]
    finding_id = f"cs-{external_id[:32]}"

    severity = _SEVERITY_MAP.get(detection.get("max_severity_displayname", "Medium"), "medium")

    mitre_predictions: Dict[str, float] = {}
    for behavior in detection.get("behaviors", []) or []:
        technique = behavior.get("technique")
        if technique:
            mitre_predictions[technique] = 0.9

    device = detection.get("device") or {}
    entity_context = {
        "src_ips": [device.get("local_ip")] if device.get("local_ip") else [],
        "hostnames": [device.get("hostname")] if device.get("hostname") else [],
        "usernames": [detection.get("user_name")] if detection.get("user_name") else [],
        "device_id": device.get("device_id"),
    }

    return {
        "finding_id": finding_id,
        "data_source": "crowdstrike",
        "external_id": external_id,
        "timestamp": detection.get("created_timestamp") or datetime.utcnow().isoformat(),
        "severity": severity,
        "status": "new",
        "title": detection.get("scenario") or "CrowdStrike Detection",
        "description": detection.get("description", ""),
        "entity_context": entity_context,
        "raw_event": detection,
        "anomaly_score": float(detection.get("max_confidence", 50)) / 100.0,
        "mitre_predictions": mitre_predictions,
        "embedding": [],
    }


def _factory() -> FederationAdapter:
    return CrowdStrikeAdapter()


register_adapter(CrowdStrikeAdapter.name, _factory)
