"""
Microsoft Defender Ingestion Service - Ingest alerts from Microsoft Defender for Endpoint.

Fetches security alerts from Microsoft Defender and converts them to findings.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations._base.config import resolve
from core.integrations._base.config_gap import (
    report_config_complete,
    report_config_gap,
)
from core.integrations._base.ids import FINDING_ID_MAX, fit_id
from core.integrations.microsoft_defender.descriptor import MICROSOFT_DEFENDER
from core.time import utcnow

logger = logging.getLogger(__name__)

# Neither call site passed a timeout, and requests defaulted to none.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

# requests followed redirects by default, httpx does not — and Microsoft's
# login endpoints redirect.
_FOLLOW_REDIRECTS = True

# The /api/alerts maximum page size.
_HISTORY_PAGE = 10000

# Machines are read a page at a time. Nothing from a row is kept here.
MACHINE_PAGE = 10000
_MACHINES_URL = "https://api.securitycenter.microsoft.com/api/machines"
_MACHINES_SCOPE = "https://api.securitycenter.microsoft.com/.default"


class MachineReadError(Exception):
    """A machines read stopped. ``kind`` is the cause; the body is not kept."""

    def __init__(self, kind: str, status_code: Optional[int] = None):
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


def _push(bucket: List[str], value: Any) -> None:
    text = str(value).strip() if value else ""
    if text and text not in bucket:
        bucket.append(text)


class MicrosoftDefenderIngestion(SIEMIngestionService):
    """Microsoft Defender ingestion service."""

    def __init__(self):
        """Initialize Microsoft Defender ingestion."""
        super().__init__()
        self.siem_name = "Microsoft Defender"
        self.config = resolve(MICROSOFT_DEFENDER)
        self.access_token = None

    def _get_access_token(self) -> Optional[str]:
        """
        Get OAuth2 access token for Microsoft Defender API.

        Returns:
            Access token, or None when credentials are not configured.

        Raises:
            Exception: when the token exchange itself fails.
        """
        try:
            tenant_id = self.config.get("tenant_id")
            client_id = self.config.get("client_id")
            client_secret = self.config.get("client_secret")

            required = {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                report_config_gap(logger, "Microsoft Defender", missing)
                return None
            report_config_complete(logger, "Microsoft Defender")

            # Get token
            token_url = (
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            )
            token_data = {
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://api.securitycenter.microsoft.com/.default",
                "grant_type": "client_credentials",
            }

            response = httpx.post(
                token_url,
                data=token_data,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=_FOLLOW_REDIRECTS,
            )
            response.raise_for_status()

            self.access_token = response.json()["access_token"]
            return self.access_token

        except Exception as e:
            logger.error(f"Error getting Microsoft Defender access token: {e}")
            # A failed token exchange is an outage, not missing configuration.
            raise

    def _machines_token(self, timeout: float) -> str:
        """The same client-credentials exchange alert collection uses.

        Logs nothing from the body. Raises ``MachineReadError``.
        """
        if self.access_token:
            return self.access_token
        tenant_id = self.config.get("tenant_id")
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")
        if not tenant_id or not client_id or not client_secret:
            raise MachineReadError("no_credential")
        try:
            response = httpx.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": _MACHINES_SCOPE,
                    "grant_type": "client_credentials",
                },
                timeout=timeout,
                follow_redirects=_FOLLOW_REDIRECTS,
            )
        except httpx.TimeoutException as exc:
            raise MachineReadError("timeout") from exc
        except httpx.HTTPError as exc:
            raise MachineReadError("unreachable") from exc
        if response.status_code >= 400:
            raise MachineReadError("http", response.status_code)
        try:
            token = response.json().get("access_token")
        except ValueError as exc:
            raise MachineReadError("parse") from exc
        if not token:
            raise MachineReadError("parse")
        self.access_token = token
        return token

    def count_machine_page(
        self, odata_filter: str, skip: int, timeout: float
    ) -> Tuple[int, int]:
        """``(ids, rows)`` for one machines page. Ids are counted and dropped.

        ``rows`` is the page length, which decides whether another page exists.
        ``$top`` is 10,000 and ``$select`` is id.
        """
        rows = self._machine_page(odata_filter, skip, timeout, "id")
        ids = sum(1 for row in rows if isinstance(row, dict) and row.get("id"))
        return ids, len(rows)

    def host_machine_page(
        self, odata_filter: str, skip: int, timeout: float
    ) -> List[Dict[str, Any]]:
        """One machines page of ``computerDnsName``, ``lastIpAddress`` and
        ``lastSeen``. The caller turns each row into Entity Keys and drops it.
        """
        return self._machine_page(
            odata_filter, skip, timeout, "computerDnsName,lastIpAddress,lastSeen"
        )

    def _machine_page(
        self, odata_filter: str, skip: int, timeout: float, select: str
    ) -> List[Any]:
        """The rows of one machines page. Logs nothing from the body."""
        token = self._machines_token(timeout)
        try:
            response = httpx.get(
                _MACHINES_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "$filter": odata_filter,
                    "$top": MACHINE_PAGE,
                    "$skip": skip,
                    "$select": select,
                },
                timeout=timeout,
                follow_redirects=_FOLLOW_REDIRECTS,
            )
        except httpx.TimeoutException as exc:
            raise MachineReadError("timeout") from exc
        except httpx.HTTPError as exc:
            raise MachineReadError("unreachable") from exc
        if response.status_code == 404:
            raise MachineReadError("empty", response.status_code)
        if response.status_code != 200:
            raise MachineReadError("http", response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise MachineReadError("parse") from exc
        rows = body.get("value") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise MachineReadError("parse")
        return rows

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch alerts from Microsoft Defender.

        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch
            oldest_first: Order by creation time ascending, so a batch that
                fills ``limit`` is a contiguous oldest-first prefix of the
                window. Federation asks for this; the default stays newest
                first for the daemon poller.

        Returns:
            List of raw alert dictionaries
        """
        try:
            # Token exchange and the alert fetch below are both blocking
            # HTTP; this method is async by interface, so offload them.
            token = await asyncio.to_thread(self._get_access_token)
            if not token:
                return []

            # Set time range
            if not start_time:
                start_time = utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = utcnow()

            # Build API request
            api_url = "https://api.securitycenter.microsoft.com/api/alerts"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            # Filter by time
            params = {
                "$filter": f"alertCreationTime ge {start_time.isoformat()}Z and alertCreationTime le {end_time.isoformat()}Z",
                "$top": limit,
                "$orderby": (
                    "alertCreationTime asc"
                    if oldest_first
                    else "alertCreationTime desc"
                ),
            }

            response = await asyncio.to_thread(
                httpx.get,
                api_url,
                headers=headers,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=_FOLLOW_REDIRECTS,
            )
            response.raise_for_status()

            alerts = response.json().get("value", [])

            logger.info(f"Fetched {len(alerts)} alerts from Microsoft Defender")
            return alerts

        except (httpx.HTTPError, httpx.InvalidURL) as e:
            logger.error(f"Microsoft Defender API error: {e}")
            # Raise, not []: federation must record the failure and keep its cursor.
            raise
        except Exception as e:
            logger.error(f"Error fetching Microsoft Defender alerts: {e}")
            raise

    async def fetch_closed_history(self, days: int) -> List[Dict[str, Any]]:
        """Resolved, classified alerts created in the last ``days``, as ticket
        rows for ``write_import`` (#3981). Pages with $skip so a long window is
        not cut at $top. Raises when credentials are missing or a call fails."""
        token = await asyncio.to_thread(self._get_access_token)
        if not token:
            raise RuntimeError("Microsoft Defender credentials are not configured")
        # Naive UTC, formatted here: an aware isoformat() would read "+00:00Z".
        end = utcnow().replace(microsecond=0)
        start = end - timedelta(days=days)
        headers = {"Authorization": f"Bearer {token}"}
        rows: List[Dict[str, Any]] = []
        skip = 0
        while True:
            params = {
                "$filter": (
                    f"alertCreationTime ge {start.isoformat()}Z"
                    f" and alertCreationTime le {end.isoformat()}Z"
                ),
                "$orderby": "alertCreationTime asc",
                # Without it evidence[] comes back empty: no IPs, users or hashes.
                "$expand": "evidence",
                "$top": _HISTORY_PAGE,
                "$skip": skip,
            }
            response = await asyncio.to_thread(
                httpx.get,
                "https://api.securitycenter.microsoft.com/api/alerts",
                headers=headers,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=_FOLLOW_REDIRECTS,
            )
            response.raise_for_status()
            page = response.json().get("value", [])
            for alert in page:
                # Legacy alerts say "Unknown" where nobody classified them.
                if alert.get("status") == "Resolved" and alert.get(
                    "classification"
                ) not in (None, "", "Unknown"):
                    rows.append(self._history_row(alert))
            if len(page) < _HISTORY_PAGE:
                break
            skip += len(page)
        logger.info(f"Read {len(rows)} resolved alerts from Microsoft Defender")
        return rows

    async def fetch_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """One alert by its stored id, or None when Defender no longer holds it."""
        token = await asyncio.to_thread(self._get_access_token)
        if not token:
            return None
        response = await asyncio.to_thread(
            httpx.get,
            "https://api.securitycenter.microsoft.com/api/alerts/"
            + quote(str(alert_id), safe=""),
            headers={"Authorization": f"Bearer {token}"},
            params={"$expand": "evidence"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=_FOLLOW_REDIRECTS,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get("id"):
            return None
        return body

    def _history_row(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        finding = self.transform_alert_to_finding(alert) or {}
        return {
            "id": alert.get("id"),
            "title": alert.get("title"),
            "disposition": alert.get("classification"),
            "opened_at": alert.get("alertCreationTime"),
            "closed_at": alert.get("resolvedTime"),
            "closed_by": alert.get("assignedTo"),
            "close_notes": alert.get("determination"),
            "severity": alert.get("severity"),
            "rule_id": alert.get("detectorId"),
            "entity_context": finding.get("entity_context") or {},
            "source_system": "microsoft_defender",
        }

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Transform Microsoft Defender alert to finding format.

        Emits the keys ``ingest_finding`` persists: description, entity_context,
        mitre_predictions. MDE often leaves description empty and puts the text
        in title.
        """
        try:
            # Alert ids run to 42 characters; "defender-" + id overflows
            # findings.finding_id String(50). external_id stays the raw id:
            # federation used to strip the prefix, and the portal link and
            # the (data_source, external_id) dedup key both use that form.
            alert_id = str(alert.get("id") or uuid.uuid4().hex[:12])
            finding_id = fit_id("defender-", alert_id, FINDING_ID_MAX)

            src_ips: List[str] = []
            domains: List[str] = []
            usernames: List[str] = []
            hostnames: List[str] = []
            file_hashes: List[str] = []

            for item in alert.get("evidence") or []:
                entity_type = (item.get("entityType") or "").lower()
                if entity_type == "ip":
                    _push(src_ips, item.get("ipAddress"))
                elif entity_type == "url":
                    _push(domains, item.get("url"))
                elif entity_type == "user":
                    _push(usernames, item.get("userPrincipalName"))
                elif entity_type == "machine":
                    _push(hostnames, item.get("deviceDnsName"))
                elif entity_type == "file":
                    _push(file_hashes, item.get("sha256"))
                    _push(file_hashes, item.get("sha1"))
                    _push(file_hashes, item.get("md5"))

            # Legacy /api/alerts carries entities at the top level with evidence[] empty.
            # Evidence-derived values stay first; _push dedupes overlaps.
            def _push_str(bucket: List[str], value: Any) -> None:
                if isinstance(value, str):
                    _push(bucket, value)

            _push_str(hostnames, alert.get("computerDnsName"))
            logged_on = alert.get("loggedOnUsers")
            for user in logged_on if isinstance(logged_on, list) else []:
                if isinstance(user, dict):
                    _push_str(usernames, user.get("userName"))
                    _push_str(usernames, user.get("accountName"))
            alert_domains = alert.get("domains")
            for domain in alert_domains if isinstance(alert_domains, list) else []:
                _push_str(domains, domain)

            title = alert.get("title") or "Microsoft Defender Alert"
            description = (alert.get("description") or "").strip() or title

            mitre_predictions = {
                str(tid): None for tid in (alert.get("mitreTechniques") or []) if tid
            }

            entity_context: Dict[str, Any] = {
                "src_ips": src_ips,
                "hostnames": hostnames,
                "usernames": usernames,
                "domains": domains,
                "file_hashes": file_hashes,
            }
            if alert.get("category"):
                entity_context["category"] = alert["category"]
            if alert.get("threatFamilyName"):
                entity_context["threat_family"] = alert["threatFamilyName"]
            # Tells a custom detection (attributable to its rule) from a built-in one.
            if (
                isinstance(alert.get("detectionSource"), str)
                and alert["detectionSource"]
            ):
                entity_context["detectionSource"] = alert["detectionSource"]

            return {
                "finding_id": finding_id,
                "external_id": alert_id,
                "data_source": "microsoft_defender",
                "timestamp": alert.get("alertCreationTime", utcnow().isoformat()),
                "severity": self.normalize_severity(alert.get("severity")),
                "title": title,
                "description": description,
                "entity_context": entity_context,
                "mitre_predictions": mitre_predictions,
            }

        except Exception as e:
            logger.error(f"Error transforming Microsoft Defender alert: {e}")
            return None
