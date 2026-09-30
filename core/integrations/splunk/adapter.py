"""Splunk federation adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.config import is_integration_enabled
from core.federation.adapters._base import (
    fresh_cursor,
    next_cursor,
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
# (matching pre-federation behavior).
_QUERIES = [
    "index=notable",
    "index=security sourcetype=*:alert*",
    "`notable`",
]

# The cursor follows when Splunk indexed an alert, not the alert's own _time,
# so one that arrives late is still read. The event-time window only has to be
# wide enough to include it.
_LATE_ARRIVAL = timedelta(hours=24)
# _indextime is whole seconds.
_CURSOR_STEP = timedelta(seconds=1)
_INDEX_TIME_FIELD = "vigil_indextime"
_CLOCK_FIELD = "vigil_now"

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
            verify_ssl=bool(cfg["verify_ssl"]),
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

        # No cold-start backfill — federation MVP design.
        start = parse_cursor_since(cursor) or since or utcnow() - timedelta(minutes=1)
        local_now = utcnow()

        # A truncated batch's query keeps the source until it drains: another
        # query answering in between would move the cursor past its backlog.
        pinned = cursor.get("query")
        queries = [pinned] if pinned in _QUERIES else _QUERIES

        # search() returns None on any error (it logs and swallows them), so a
        # query failed if it returned None or raised. An empty list ran and
        # found nothing; it still falls through, since on non-ES installs
        # `index=notable` is empty by design and the fallbacks must be reached.
        events: List[Dict[str, Any]] = []
        answered_by: Optional[str] = None
        splunk_now: Optional[datetime] = None
        answered = False
        last_error: Optional[Exception] = None
        for base in queries:
            try:
                # search() polls its job with time.sleep for up to ~60s.
                results = await asyncio.to_thread(
                    svc.search,
                    query=_index_time_query(base, start, max_items),
                    earliest_time=str(math.floor(_epoch(start - _LATE_ARRIVAL))),
                    latest_time="now",
                    max_count=max_items + 1,  # the clock row
                )
            except Exception as e:
                logger.warning("Splunk query failed (%s): %s", base, e)
                last_error = e
                continue
            if results is None:
                logger.warning(
                    "Splunk query failed (%s): search returned no result", base
                )
                continue
            answered = True
            rows, clock = _split_clock_row(results)
            splunk_now = clock or splunk_now
            if rows:
                events, answered_by = rows, base
                break
        # Every query failed: raise so the runner records a failure and keeps
        # the cursor, instead of advancing it past the outage window.
        if not answered:
            detail = f": {last_error}" if last_error is not None else ""
            raise RuntimeError(f"Splunk: every search query failed{detail}")

        # _indextime is stamped by Splunk's clock, so the cursor must be too:
        # with ours, a Splunk running behind would index alerts behind it.
        now = splunk_now
        if now is None:
            logger.warning(
                "Federation %s: search answer carried no Splunk clock; placing the "
                "cursor by this host's clock",
                self.name,
            )
            now = local_now

        events = events[:max_items]
        findings = []
        for event in events:
            f = _splunk_event_to_finding(event)
            if f is not None:
                findings.append(f)

        cursor, more = next_cursor(
            (_index_time(e) for e in events),
            truncated=len(events) >= max_items,
            start=start,
            now=now,
            step=_CURSOR_STEP,
            source=self.name,
        )
        if more and answered_by:
            cursor["query"] = answered_by
        return FetchResult(findings=findings, cursor=cursor, truncated=more)


def _epoch(when: datetime) -> float:
    return when.replace(tzinfo=timezone.utc).timestamp()


def _index_time_query(base: str, start: datetime, limit: int) -> str:
    """Alerts Splunk indexed from ``start`` to its own now, oldest first.

    The index-time bounds go in front: the ``notable`` macro expands to a
    search with pipes, so anything appended after it would bind to its last
    command. The appended row carries Splunk's clock, and is there even when
    no alert is.
    """
    return (
        f"_index_earliest={math.floor(_epoch(start))} _index_latest=now {base} "
        f"| eval {_INDEX_TIME_FIELD}=_indextime "
        f"| sort 0 {_INDEX_TIME_FIELD} | head {limit} "
        f"| appendpipe [stats count | eval {_CLOCK_FIELD}=now()]"
    )


def _split_clock_row(
    results: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[datetime]]:
    rows = [r for r in results if _CLOCK_FIELD not in r]
    clocks = [_epoch_field(r, _CLOCK_FIELD) for r in results if _CLOCK_FIELD in r]
    return rows, next((c for c in clocks if c is not None), None)


def _index_time(event: Dict[str, Any]) -> Optional[datetime]:
    return _epoch_field(event, _INDEX_TIME_FIELD)


def _epoch_field(row: Dict[str, Any], field: str) -> Optional[datetime]:
    try:
        seconds = float(row[field])
    except (KeyError, TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


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
