"""
Microsoft Defender Ingestion Service - Ingest alerts from Microsoft Defender for Endpoint.

Fetches security alerts from Microsoft Defender and converts them to findings.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from core.config import get_integration_config
from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.time import utcnow

logger = logging.getLogger(__name__)

# Neither call site passed a timeout, and requests defaulted to none.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

# requests followed redirects by default, httpx does not — and Microsoft's
# login endpoints redirect.
_FOLLOW_REDIRECTS = True


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
        self.config = get_integration_config("microsoft-defender")
        self.access_token = None

    def _get_access_token(self) -> Optional[str]:
        """
        Get OAuth2 access token for Microsoft Defender API.

        Returns:
            Access token or None
        """
        try:
            tenant_id = self.config.get("tenant_id")
            client_id = self.config.get("client_id")
            client_secret = self.config.get("client_secret")

            if not all([tenant_id, client_id, client_secret]):
                logger.error("Microsoft Defender configuration incomplete")
                return None

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
            return None

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch alerts from Microsoft Defender.

        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch

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
                "$orderby": "alertCreationTime desc",
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
            return []
        except Exception as e:
            logger.error(f"Error fetching Microsoft Defender alerts: {e}")
            return []

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
            finding_id = f"defender-{alert.get('id', uuid.uuid4().hex[:12])}"

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

            title = alert.get("title") or "Microsoft Defender Alert"
            description = (alert.get("description") or "").strip() or title

            mitre_predictions = {
                str(tid): 1.0 for tid in (alert.get("mitreTechniques") or []) if tid
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

            return {
                "finding_id": finding_id,
                "data_source": "microsoft_defender",
                "timestamp": alert.get("alertCreationTime", utcnow().isoformat()),
                "severity": self.normalize_severity(alert.get("severity")),
                "status": "new",
                "title": title,
                "description": description,
                "entity_context": entity_context,
                "mitre_predictions": mitre_predictions,
            }

        except Exception as e:
            logger.error(f"Error transforming Microsoft Defender alert: {e}")
            return None
