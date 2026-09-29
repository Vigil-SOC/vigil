"""CrowdStrike Falcon federation adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from core.config import is_integration_enabled
from core.federation.adapters._base import (
    fresh_cursor,
    next_cursor,
    parse_alert_time,
    parse_cursor_since,
)
from core.federation.contract import (
    FederationAdapter,
    FetchResult,
    register_adapter,
)
from core.integrations._base.config import resolve
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.time import utcnow

logger = logging.getLogger(__name__)

# The details call takes at most 100 IDs, so a larger batch would be cut
# short without looking full.
_DETAILS_BATCH = 100
_CURSOR_STEP = timedelta(milliseconds=1)

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
            from core.integrations.crowdstrike.client import CrowdStrikeService

            # resolve() reads client_secret from the secrets store; unset fields are None.
            cfg = resolve(CROWDSTRIKE)
            self._service = CrowdStrikeService(
                client_id=cfg["client_id"] or "",
                client_secret=cfg["client_secret"] or "",
                base_url=cfg["base_url"] or "https://api.crowdstrike.com",
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
            cutoff = utcnow() - timedelta(minutes=1)

        limit = min(max_items, _DETAILS_BATCH)
        now = utcnow()
        detections = await asyncio.to_thread(
            svc.get_detections,
            filter_query=f"created_timestamp:>='{cutoff.isoformat()}Z'",
            limit=limit,
            sort="created_timestamp|asc",
        )
        if detections is None:
            # Raised so the runner records a failure and keeps the cursor.
            raise RuntimeError("CrowdStrike detections query failed")

        # The details call does not keep the query's order.
        detections = sorted(
            detections[:limit],
            key=lambda d: parse_alert_time(d.get("created_timestamp")) or now,
        )
        findings = []
        for det in detections:
            f = _detection_to_finding(det)
            if f is not None:
                findings.append(f)

        cursor, more = next_cursor(
            (parse_alert_time(d.get("created_timestamp")) for d in detections),
            truncated=len(detections) >= limit,
            start=cutoff,
            now=now,
            step=_CURSOR_STEP,
            source=self.name,
        )
        return FetchResult(findings=findings, cursor=cursor, truncated=more)


def _detection_to_finding(detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    detection_id = detection.get("detection_id", "")
    if not detection_id:
        return None

    external_id = str(detection_id)[:128]
    finding_id = f"cs-{external_id[:32]}"

    severity = _SEVERITY_MAP.get(
        detection.get("max_severity_displayname", "Medium"), "medium"
    )

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
        "timestamp": detection.get("created_timestamp") or utcnow().isoformat(),
        "severity": severity,
        "status": "new",
        "title": detection.get("scenario") or "CrowdStrike Detection",
        "description": detection.get("description", ""),
        "entity_context": entity_context,
        "raw_event": detection,
        "anomaly_score": float(detection.get("max_confidence", 50)) / 100.0,
        "mitre_predictions": mitre_predictions,
    }


def _factory() -> FederationAdapter:
    return CrowdStrikeAdapter()


register_adapter(CrowdStrikeAdapter.name, _factory)
