"""Splunk federation adapter."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
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
from core.integrations.splunk.descriptor import SPLUNK
from core.time import utcnow

logger = logging.getLogger(__name__)

# Search candidates ported from the original poller.py loop. We try the more
# specific notable index first, falling back to broader queries if it's empty
# (matching pre-federation behavior). Oldest first: when the window holds more
# than max_items events, the ones left out must be the newest (#1571).
_QUERIES = [
    "index=notable | sort 0 _time | head {limit}",
    "index=security sourcetype=*:alert* | sort 0 _time | head {limit}",
    "`notable` | sort 0 _time | head {limit}",
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
        from core.integrations.splunk.client import SplunkService

        # A configured source whose service cannot be built is failing, not
        # empty: let the error reach the runner so the cursor is kept.
        # resolve() reads the password from the secrets store; unset fields are None.
        cfg = resolve(SPLUNK)
        self._service = SplunkService(
            server_url=cfg["server_url"] or "",
            username=cfg["username"] or "",
            password=cfg["password"] or "",
            verify_ssl=cfg["verify_ssl"],
            ca_cert_path=cfg["ca_cert_path"],
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
        # Use cursor's last_poll_at when available; otherwise "now" sentinel
        # (no cold-start backfill — federation MVP design).
        last = parse_cursor_since(cursor) or since
        if last is not None:
            # Absolute epoch seconds: a relative "-Nm" is rounded up to the
            # minute, which widens the window behind a mid-minute cursor and
            # can re-read the same oldest page forever on a full batch.
            earliest_time = f"{last.replace(tzinfo=timezone.utc).timestamp():.3f}"
        else:
            # First run: tiny window so we don't replay history.
            earliest_time = "-1m"
            last = now - timedelta(minutes=1)

        # search() returns None on any error (it logs and swallows them), so a
        # query failed if it returned None or raised. An empty list ran and
        # found nothing; it falls through, since on non-ES installs
        # `index=notable` is empty by design and the fallbacks must be reached.
        # A failed query fails the tick (the runner keeps the cursor and records
        # last_error), else a fallback's results would hide the events it missed.
        # The one exception is the last query: the `notable` macro is undefined
        # without Enterprise Security, so it fails at job creation there. As a
        # last resort after empty queries it cannot discard anything.
        events = []
        for i, query_tmpl in enumerate(_QUERIES):
            query = query_tmpl.format(limit=max_items)
            try:
                # search() polls its job with time.sleep for up to ~60s.
                results = await asyncio.to_thread(
                    svc.search,
                    query=query,
                    earliest_time=earliest_time,
                    latest_time="now",
                    max_count=max_items,
                )
                error = "search returned no result" if results is None else None
            except Exception as e:
                results, error = None, str(e)
            if results is None:
                logger.warning("Splunk query failed (%s): %s", query, error)
                if i < len(_QUERIES) - 1:
                    raise RuntimeError(f"Splunk: query failed ({query}): {error}")
                continue
            if results:
                events = results
                break

        events = events[:max_items]
        findings = []
        for event in events:
            f = _splunk_event_to_finding(event)
            if f is not None:
                findings.append(f)

        # A full batch may have left newer events behind: stop at the newest
        # returned _time; the next tick re-reads it and dedup absorbs it.
        cursor_out = len(events) >= max_items and full_batch_cursor(
            [parse_alert_time(e.get("_time")) for e in events],
            start=last,
            now=now,
            source=self.name,
            count=len(events),
        )
        return FetchResult(findings=findings, cursor=cursor_out or fresh_cursor())


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
        "timestamp": event.get("_time") or utcnow().isoformat(),
        "severity": severity,
        "status": "new",
        "title": event.get("search_name") or event.get("rule_name") or "Splunk Alert",
        "description": event.get("description") or event.get("_raw", "")[:500],
        "entity_context": entity_context,
        "raw_event": event,
        "anomaly_score": 0.5,
        "mitre_predictions": {},
    }


def _factory() -> FederationAdapter:
    return SplunkAdapter()


register_adapter(SplunkAdapter.name, _factory)
