"""Shared adapter for sources that already implement ``SIEMIngestionService``.

The four cloud SIEMs (Azure Sentinel, AWS Security Hub, Microsoft Defender,
Elastic Security) all expose ``async fetch_alerts(start_time, limit)`` and
``transform_alert_to_finding(alert)`` via the
:class:`core.ingestion.siem_ingestion_service.SIEMIngestionService` base class. This
adapter wraps that contract so each concrete source needs only a one-line
factory module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.config import is_integration_enabled
from core.federation.adapters._base import (
    drained_cursor,
    fresh_cursor,
    next_cursor,
    parse_cursor_since,
)
from core.federation.contract import FetchResult
from core.time import utcnow

logger = logging.getLogger(__name__)

# How far past a stuck instant the cursor steps. Elastic's ``date`` fields and
# Security Hub's ``CreatedAt`` resolve to the millisecond, so a smaller step
# would round back to the same instant on their side and re-read the same page
# every tick. Defender and Sentinel record finer times, so on those two a step
# also skips alerts later in the same millisecond.
_CURSOR_STEP = timedelta(milliseconds=1)


class SIEMIngestionAdapter:
    """Adapter wrapping any ``SIEMIngestionService`` subclass.

    The adapter is intentionally not generic over the integration_id — the
    caller passes the source name, integration id (for the ``is_configured``
    check), default interval, and a service factory. This keeps each concrete
    adapter file under 30 lines.
    """

    def __init__(
        self,
        *,
        name: str,
        integration_id: str,
        default_interval: int,
        service_factory: Callable[[], Any],
        external_id_prefix: str,
        alert_time: Optional[Callable[[Dict[str, Any]], Optional[datetime]]] = None,
        alert_id: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self.name = name
        self._integration_id = integration_id
        self._default_interval = default_interval
        self._service_factory = service_factory
        self._service: Optional[Any] = None
        self._external_id_prefix = external_id_prefix
        # Reads a raw alert's creation time as naive UTC. With it, a full batch
        # advances the cursor only to the newest alert returned; without it the
        # cursor goes to now, as it did before, and a full batch skips the rest
        # of its window.
        self._alert_time = alert_time
        # For a service whose fetch_alerts takes after_id: resumes strictly
        # after the last alert of a full batch rather than stepping past its
        # instant.
        self._alert_id = alert_id

    def is_configured(self) -> bool:
        return is_integration_enabled(self._integration_id)

    def default_interval(self) -> int:
        return self._default_interval

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.is_configured():
            return None
        # A configured source whose service cannot be built is failing, not
        # empty: let the error reach the runner so the cursor is kept.
        self._service = self._service_factory()
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

        start_time = parse_cursor_since(cursor) or since
        if start_time is None:
            # First run: small window so we don't backfill on enable.
            start_time = utcnow() - timedelta(minutes=1)

        # A raised fetch reaches the runner, which records the failure and keeps
        # the cursor; swallowing it here would advance the cursor past alerts
        # the source never returned.
        #
        # oldest_first: when the batch fills max_items, the alerts that do not
        # fit must be the newest ones so the next tick, starting from the newest
        # returned time, picks them up. Newest-first would drop the oldest of
        # the window for good.
        # Taken before the fetch: the cursor never moves past the "now" the
        # pre-change code would have stored, even if a source returns an alert
        # stamped ahead of our clock.
        now = utcnow()
        after = cursor.get("after_id") if self._alert_id else None
        resume = {"after_id": after} if self._alert_id else {}
        alerts = list(
            await svc.fetch_alerts(
                start_time=start_time, limit=max_items, oldest_first=True, **resume
            )
            or []
        )
        truncated = len(alerts) >= max_items
        alerts = alerts[:max_items]

        findings = []
        for alert in alerts:
            try:
                finding = svc.transform_alert_to_finding(alert)
            except Exception as e:
                logger.debug("%s transform failed: %s", self.name, e)
                continue
            if not finding:
                continue
            # Backfill external_id from the prefix-stripped finding_id when
            # the underlying service doesn't set it explicitly. We need
            # external_id populated for the (data_source, external_id) UNIQUE
            # dedup index to do its job.
            if not finding.get("external_id"):
                fid = finding.get("finding_id", "")
                prefix = f"{self._external_id_prefix}-"
                if fid.startswith(prefix):
                    finding["external_id"] = fid[len(prefix) :]
                else:
                    finding["external_id"] = fid
            findings.append(finding)

        if self._alert_time is None:
            return FetchResult(findings=findings, cursor=drained_cursor(now))
        cursor, more = next_cursor(
            self._alert_times(alerts),
            truncated=truncated,
            start=start_time,
            now=now,
            step=_CURSOR_STEP,
            source=self.name,
            ids=[self._alert_id(a) for a in alerts] if self._alert_id else None,
            after=after,
        )
        return FetchResult(findings=findings, cursor=cursor, truncated=more)

    def _alert_times(self, alerts: list) -> List[Optional[datetime]]:
        times: List[Optional[datetime]] = []
        for alert in alerts:
            try:
                when = self._alert_time(alert)
            except Exception as e:  # a malformed record must not fail the poll
                logger.debug("%s: alert time unreadable: %s", self.name, e)
                when = None
            if when is not None and when.tzinfo is not None:
                when = when.astimezone(timezone.utc).replace(tzinfo=None)
            times.append(when)
        return times
