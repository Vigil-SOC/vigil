"""Splunk federation adapter."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import get_integration_config, is_integration_enabled
from daemon.federation.adapters._base import fresh_cursor, parse_cursor_since
from daemon.federation.registry import (
    FederationAdapter,
    FetchResult,
    register_adapter,
)

logger = logging.getLogger(__name__)

# Search candidates ported from the original poller.py loop. We try the more
# specific notable index first, falling back to broader queries if it's empty
# (matching pre-federation behavior).
_QUERIES = [
    "index=notable | head {limit}",
    "index=security sourcetype=*:alert* | head {limit}",
    "`notable` | head {limit}",
]

_SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "low",
    "informational": "low",
}


class SplunkAdapter:
    name = "splunk"

    def __init__(self) -> None:
        self._service = None

    def is_configured(self) -> bool:
        return is_integration_enabled("splunk")

    def default_interval(self) -> int:
        return 300  # 5 min — SIEM cadence

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.is_configured():
            return None
        try:
            from services.splunk_service import SplunkService

            cfg = get_integration_config("splunk")
            self._service = SplunkService(
                server_url=cfg.get("server_url", ""),
                username=cfg.get("username", ""),
                password=cfg.get("password", ""),
                verify_ssl=cfg.get("verify_ssl", False),
            )
        except Exception as e:
            logger.warning("Splunk service init failed: %s", e)
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

        # Use cursor's last_poll_at when available; otherwise "now" sentinel
        # (no cold-start backfill — see CLAUDE.md / federation MVP design).
        last = parse_cursor_since(cursor) or since
        if last is not None:
            # Convert to relative Splunk earliest_time (rounded up to minute)
            delta_minutes = max(int((datetime.utcnow() - last).total_seconds() // 60) + 1, 1)
            earliest_time = f"-{delta_minutes}m"
        else:
            # First run: tiny window so we don't replay history.
            earliest_time = "-1m"

        events = []
        for query_tmpl in _QUERIES:
            query = query_tmpl.format(limit=max_items)
            try:
                results = svc.search(
                    query=query,
                    earliest_time=earliest_time,
                    latest_time="now",
                    max_count=max_items,
                )
                if results:
                    events = results
                    break
            except Exception as e:
                logger.debug("Splunk query failed (%s): %s", query, e)
                continue

        findings = []
        for event in events[:max_items]:
            f = _splunk_event_to_finding(event)
            if f is not None:
                findings.append(f)

        return FetchResult(findings=findings, cursor=fresh_cursor())


def _splunk_event_to_finding(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_id = event.get("_cd") or event.get("event_id") or uuid.uuid4().hex
    external_id = str(raw_id)[:64]
    finding_id = f"splunk-{external_id[:32]}"

    severity_raw = (event.get("urgency") or event.get("severity") or "medium").lower()
    severity = _SEVERITY_MAP.get(severity_raw, "medium")

    entity_context: Dict[str, Any] = {
        "src_ips": [],
        "dest_ips": [],
        "hostnames": [],
        "usernames": [],
    }
    for f in ("src_ip", "src", "source_ip"):
        if event.get(f):
            entity_context["src_ips"].append(event[f])
    for f in ("dest_ip", "dest", "destination_ip"):
        if event.get(f):
            entity_context["dest_ips"].append(event[f])
    for f in ("host", "hostname", "src_host", "dest_host"):
        if event.get(f):
            entity_context["hostnames"].append(event[f])
    for f in ("user", "username", "src_user"):
        if event.get(f):
            entity_context["usernames"].append(event[f])

    return {
        "finding_id": finding_id,
        "data_source": "splunk",
        "external_id": external_id,
        "timestamp": event.get("_time") or datetime.utcnow().isoformat(),
        "severity": severity,
        "status": "new",
        "title": event.get("search_name") or event.get("rule_name") or "Splunk Alert",
        "description": event.get("description") or event.get("_raw", "")[:500],
        "entity_context": entity_context,
        "raw_event": event,
        "anomaly_score": 0.5,
        "mitre_predictions": {},
        "embedding": [],
    }


def _factory() -> FederationAdapter:
    return SplunkAdapter()


register_adapter(SplunkAdapter.name, _factory)
