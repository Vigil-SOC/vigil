"""CrowdStrike Falcon federation adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from core.config import is_integration_enabled
from core.federation.adapters._base import (
    fresh_cursor,
    full_batch_cursor,
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
        from core.integrations.crowdstrike.client import CrowdStrikeService

        # A configured source whose service cannot be built is failing, not
        # empty: let the error reach the runner so the cursor is kept.
        # resolve() reads client_secret from the secrets store; unset fields are None.
        cfg = resolve(CROWDSTRIKE)
        self._service = CrowdStrikeService(
            client_id=cfg["client_id"] or "",
            client_secret=cfg["client_secret"] or "",
            base_url=cfg["base_url"] or "https://api.crowdstrike.com",
        )
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

        # Taken before the fetch: the cursor never moves past this instant.
        now = utcnow()
        cutoff = parse_cursor_since(cursor) or since
        if cutoff is None:
            # First run: small window, no backfill.
            cutoff = now - timedelta(minutes=1)

        # The Detects ID query cannot sort on created_timestamp (its documented
        # sort keys are first_behavior, last_behavior, max_severity,
        # max_confidence, adversary_id, devices.hostname), so "oldest first" is
        # done here: read every detection in the window (limit=None pages the
        # IDs and summarises them in chunks), sort by created_timestamp, and
        # keep the oldest max_items. When the window overflows, the ones left
        # out are the newest, and the cursor stops at the newest one kept.
        detections = await asyncio.to_thread(
            svc.get_detections,
            filter_query=f"created_timestamp:>='{cutoff.isoformat()}Z'",
            limit=None,
        )
        if detections is None:
            # Raised so the runner records a failure and keeps the cursor.
            detail = getattr(svc, "last_error", None)
            raise RuntimeError(
                "CrowdStrike detections query failed"
                + (f": {detail}" if detail else "")
            )

        # An unreadable time sorts first: the cursor cannot track it, so it
        # must not be the one left behind.
        by_time = sorted(
            ((parse_alert_time(d.get("created_timestamp")), d) for d in detections),
            key=lambda td: td[0] or datetime.min,
        )
        truncated = len(by_time) >= max_items
        by_time = by_time[:max_items]

        findings = []
        for _, det in by_time:
            f = _detection_to_finding(det)
            if f is not None:
                findings.append(f)

        cursor_out = truncated and full_batch_cursor(
            [t for t, _ in by_time],
            start=cutoff,
            now=now,
            source=self.name,
            count=len(by_time),
        )
        return FetchResult(findings=findings, cursor=cursor_out or fresh_cursor())


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
