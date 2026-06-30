"""SentinelOne Ingestion Service — fetch threats from SentinelOne REST API v2.1."""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from core.config import get_integration_config
from services.siem_ingestion_service import SIEMIngestionService

logger = logging.getLogger(__name__)

_CONFIDENCE_SEVERITY = {
    "malicious": "high",
    "suspicious": "medium",
}

# Bump to critical for known high-impact classification types
_CRITICAL_CLASSIFICATIONS = frozenset(
    {"Ransomware", "Rootkit", "Exploit", "ExploitKit"}
)


def _extract_mitigation(raw: Any) -> str:
    """Return a plain mitigation-status string from either API layout.

    v2.1 returns a list of dicts with a ``status`` key; v2.0 returns a
    plain string. Both may appear at root level or inside ``threatInfo``.
    """
    if isinstance(raw, list) and raw:
        first = raw[0]
        return first.get("status", "") if isinstance(first, dict) else str(first)
    if isinstance(raw, str):
        return raw
    return ""


class SentinelOneIngestion(SIEMIngestionService):
    """Fetches threats from SentinelOne Management API v2.1.

    Credentials are read from the integration config stored by the Settings UI:
      console_url — the management console base URL
      api_token   — the API token with Threats → View permission
    """

    def __init__(self) -> None:
        super().__init__()
        self.siem_name = "SentinelOne"

    def _credentials(self):
        """Read credentials fresh each call so secrets restored after init work."""
        cfg = get_integration_config("sentinelone")
        url = (
            cfg.get("console_url")
            or cfg.get("url")
            or os.environ.get("SENTINELONE_CONSOLE_URL")
            or ""
        ).rstrip("/")
        token = (
            cfg.get("api_token")
            or cfg.get("token")
            or os.environ.get("SENTINELONE_API_TOKEN")
            or ""
        )
        return url, token

    def _headers(self) -> Dict[str, str]:
        _, token = self._credentials()
        return {
            "Authorization": f"ApiToken {token}",
            "Content-Type": "application/json",
        }

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch threats from SentinelOne API v2.1.

        Args:
            start_time: Only return threats created after this time.
            end_time:   Only return threats created before this time.
            limit:      Maximum number of threats to return (capped at 1000).

        Returns:
            List of raw threat dicts as returned by the API.
        """
        url, token = self._credentials()
        if not url or not token:
            logger.warning(
                "SentinelOne not configured (missing console_url or api_token)"
            )
            return []

        params: Dict[str, Any] = {
            "limit": min(limit, 1000),
            "sortBy": "createdAt",
            "sortOrder": "asc",
        }
        if start_time:
            params["createdAt__gte"] = start_time.strftime(
                "%Y-%m-%dT%H:%M:%S.000000Z"
            )
        if end_time:
            params["createdAt__lte"] = end_time.strftime(
                "%Y-%m-%dT%H:%M:%S.000000Z"
            )

        try:
            url = f"{url}/web/api/v2.1/threats"
            resp = await asyncio.to_thread(
                requests.get,
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            threats = body.get("data", [])
            if not threats:
                logger.debug(
                    "SentinelOne returned 0 threats. pagination=%s",
                    body.get("pagination"),
                )
            logger.info("Fetched %d threats from SentinelOne", len(threats))
            return threats
        except Exception as e:
            logger.error("SentinelOne fetch_alerts failed: %s", e)
            return []

    def transform_alert_to_finding(
        self, threat: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Normalize a raw SentinelOne threat into the Vigil finding format.

        Handles both the v2.1 nested layout (threatInfo / agentRealtimeInfo)
        and the older flat layout where fields appear at root level, using safe
        fallback chains throughout.
        """
        threat_id = threat.get("id")
        if not threat_id:
            return None

        external_id = str(threat_id)
        finding_id = f"s1-{external_id}"

        # v2.1 nested objects — also check root level as fallback
        threat_info = threat.get("threatInfo") or {}
        agent_info = threat.get("agentRealtimeInfo") or {}
        agent_detection = threat.get("agentDetectionInfo") or {}

        threat_name = (
            threat_info.get("threatName")
            or threat.get("threatName")
            or "SentinelOne Threat"
        )
        classification = (
            threat_info.get("classification")
            or threat.get("classification")
            or ""
        )
        confidence = (
            (
                threat_info.get("confidenceLevel")
                or threat.get("confidenceLevel")
                or "n/a"
            )
            .lower()
            .strip()
        )
        created_at = (
            threat_info.get("createdAt")
            or threat.get("createdAt")
            or threat.get("createdDate")
            or datetime.utcnow().isoformat()
        )

        # Severity: malicious → high, bumped to critical for severe families
        severity = _CONFIDENCE_SEVERITY.get(confidence, "low")
        if severity == "high" and classification in _CRITICAL_CLASSIFICATIONS:
            severity = "critical"

        # mitigationStatus: list in v2.1, string in v2.0; check both locations
        mit_str = _extract_mitigation(
            threat.get("mitigationStatus") or threat_info.get("mitigationStatus")
        )

        # Entity extraction — check nested then flat
        hostname = (
            agent_info.get("agentComputerName")
            or threat.get("agentComputerName")
            or ""
        )
        username = (
            threat.get("username")
            or threat_info.get("processUser")
            or agent_info.get("username")
            or ""
        )
        ip = (
            agent_detection.get("agentIpV4")
            or agent_info.get("agentIp")
            or threat.get("agentIp")
            or ""
        )

        # File hashes — field names differ between API versions
        md5 = (
            threat_info.get("md5")
            or threat_info.get("fileMd5")
            or threat.get("md5")
            or ""
        )
        sha256 = (
            threat_info.get("sha256")
            or threat_info.get("fileSha256")
            or threat.get("sha256")
            or ""
        )
        file_hashes = [h for h in [md5, sha256] if h]

        entity_context: Dict[str, Any] = {
            "src_ips": [ip] if ip else [],
            "dest_ips": [],
            "hostnames": [hostname] if hostname else [],
            "usernames": [username] if username else [],
            "file_hashes": file_hashes,
        }

        description = (
            f"{classification} detected on {hostname}: {threat_name}"
            f" (confidence: {confidence}, mitigation: {mit_str or 'unknown'})"
        ).strip(": ")

        return {
            "finding_id": finding_id,
            "data_source": "sentinelone",
            "external_id": external_id,
            "timestamp": created_at,
            "severity": severity,
            "status": "new",
            "title": threat_name or classification or "SentinelOne Threat",
            "description": description[:500],
            "entity_context": entity_context,
            "raw_event": threat,
            "anomaly_score": 0.8 if confidence == "malicious" else 0.4,
            "mitre_predictions": {},
            "embedding": [],
            "metadata": {
                "s1_threat_id": external_id,
                "classification": classification,
                "confidence_level": confidence,
                "mitigation_status": mit_str,
                "agent_id": (
                    agent_info.get("agentId") or threat.get("agentId") or ""
                ),
            },
        }
