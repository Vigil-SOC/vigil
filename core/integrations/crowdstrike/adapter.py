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
from core.ingestion.ingestion_service import _optional_float
from core.integrations._base.config import resolve
from core.integrations._base.ids import FINDING_ID_MAX, fit_id
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.time import utcnow

logger = logging.getLogger(__name__)


def _confidence_to_score(confidence: Any) -> Optional[float]:
    """CrowdStrike ``confidence`` is 0–100. Scale only a parsed number."""
    parsed = _optional_float(confidence)
    if parsed is None:
        return None
    return parsed / 100.0


# Alerts query default order is newest first. Oldest-first is one page of the
# backlog; the cursor walks the rest.
_OLDEST_FIRST = "created_timestamp|asc"


def endpoint_alert_filter(since: datetime) -> str:
    """Endpoint alerts created at or after ``since`` (naive UTC).

    FQL AND is ``+``. Other Falcon products stay out: this is the endpoint
    alerts the Detects API used to return.
    """
    return f"product:'epp'+created_timestamp:>='{since.isoformat()}Z'"


_SEVERITY_MAP = {
    "Critical": "critical",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    # Informational ranks below low. Matched as CrowdStrike sends it.
    "Informational": "info",
}


def crowdstrike_severity(alert: Dict[str, Any]) -> str:
    """Stored severity for one CrowdStrike alert.

    ``severity_name`` is matched case-sensitively. ``Informational`` is
    ``info``. A missing name is Medium, and any other unrecognised value
    is ``medium``. Federation and the older poll both call this.
    """
    raw = alert.get("severity_name", "Medium")
    return _SEVERITY_MAP.get(raw, "medium")


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
        cursor: Dict[str, Any],
        max_items: int,
    ) -> FetchResult:
        svc = self._get_service()
        if svc is None:
            return FetchResult(findings=[], cursor=fresh_cursor())

        # Taken before the fetch: the cursor never moves past this instant.
        now = utcnow()
        cutoff = parse_cursor_since(cursor)
        if cutoff is None:
            # First run: small window, no backfill.
            cutoff = now - timedelta(minutes=1)

        # One oldest page. A full page leaves the rest of the window for the
        # next tick (full_batch_cursor). Reading newest-first would skip it.
        alerts = await asyncio.to_thread(
            svc.get_alerts,
            filter_query=endpoint_alert_filter(cutoff),
            limit=max_items,
            sort=_OLDEST_FIRST,
        )
        if alerts is None:
            # Raised so the runner records a failure and keeps the cursor.
            detail = getattr(svc, "last_error", None)
            raise RuntimeError(
                "CrowdStrike alerts query failed" + (f": {detail}" if detail else "")
            )

        truncated = len(alerts) >= max_items
        alerts = alerts[:max_items]

        findings = []
        for alert in alerts:
            finding = _alert_to_finding(alert)
            if finding is not None:
                findings.append(finding)

        cursor_out = truncated and full_batch_cursor(
            [parse_alert_time(alert.get("created_timestamp")) for alert in alerts],
            start=cutoff,
            now=now,
            source=self.name,
            count=len(alerts),
        )
        return FetchResult(findings=findings, cursor=cursor_out or fresh_cursor())


def _technique_keys(alert: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """``technique``, plus each ``mitre_attack`` entry's technique and id."""
    keys: Dict[str, Optional[float]] = {}

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            keys[value] = None

    add(alert.get("technique"))
    for entry in alert.get("mitre_attack") or []:
        if isinstance(entry, str):
            add(entry)
            continue
        if isinstance(entry, dict):
            add(entry.get("technique"))
            add(entry.get("technique_id"))
    return keys


def _alert_value(alert: Dict[str, Any], name: str) -> Any:
    """Flat field, else the same field on ``device``.

    Endpoint alerts still nest hostname, local IP and device id on ``device``.
    """
    value = alert.get(name)
    if value:
        return value
    device = alert.get("device")
    if isinstance(device, dict):
        nested = device.get(name)
        if nested:
            return nested
    return None


def _alert_to_finding(alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    composite_id = str(alert.get("composite_id") or "")
    if not composite_id:
        return None

    hostname = _alert_value(alert, "hostname")
    local_ip = _alert_value(alert, "local_ip")
    user_name = _alert_value(alert, "user_name")
    # The whole composite_id. findings.external_id is 255, wide enough to
    # keep it, and (data_source, external_id) dedups on that id.
    return {
        "finding_id": fit_id("cs-", composite_id, FINDING_ID_MAX),
        "data_source": "crowdstrike",
        "external_id": composite_id,
        "timestamp": alert.get("created_timestamp") or utcnow().isoformat(),
        "severity": crowdstrike_severity(alert),
        "title": alert.get("display_name") or alert.get("name") or "CrowdStrike Alert",
        "description": alert.get("description", ""),
        "entity_context": {
            "src_ips": [local_ip] if local_ip else [],
            "hostnames": [hostname] if hostname else [],
            "usernames": [user_name] if user_name else [],
            "device_id": _alert_value(alert, "device_id"),
        },
        "raw_data": alert,
        "anomaly_score": _confidence_to_score(alert.get("confidence")),
        "mitre_predictions": _technique_keys(alert),
    }


def _factory() -> FederationAdapter:
    return CrowdStrikeAdapter()


register_adapter(CrowdStrikeAdapter.name, _factory)
