"""CrowdStrike Falcon API service for detection polling and host management."""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from core.time import utcnow

logger = logging.getLogger(__name__)

# Client-level floor; every call site also passes timeout=30 explicitly.
DEFAULT_TIMEOUT = 30.0

# Falcon caps: IDs per ID-query page, and IDs per summaries request.
_ID_PAGE_MAX = 9999
_SUMMARIES_MAX = 1000

# requests followed redirects by default; httpx does not.
_FOLLOW_REDIRECTS = True


class CrowdStrikeService:
    """Service for interacting with CrowdStrike Falcon API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.crowdstrike.com",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        # Detail of the last failed get_detections call (e.g. "HTTP 500 ..."),
        # so callers can report why it returned None.
        self.last_error: Optional[str] = None
        self.session = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=_FOLLOW_REDIRECTS,
        )

    def _ensure_authenticated(self) -> bool:
        """Ensure we have a valid access token."""
        if self.access_token and self.token_expiry:
            if utcnow() < self.token_expiry:
                return True
        return self._authenticate()

    def _authenticate(self) -> bool:
        """Authenticate with CrowdStrike OAuth2."""
        try:
            response = self.session.post(
                f"{self.base_url}/oauth2/token",
                data={"client_id": self.client_id, "client_secret": self.client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )

            if response.status_code == 201:
                data = response.json()
                self.access_token = data.get("access_token")
                expires_in = data.get("expires_in", 1800)
                self.token_expiry = utcnow() + timedelta(seconds=expires_in - 60)
                self.session.headers.update(
                    {"Authorization": f"Bearer {self.access_token}"}
                )
                logger.info("CrowdStrike authentication successful")
                return True
            else:
                logger.error(f"CrowdStrike auth failed: {response.status_code}")
                self.last_error = f"authentication failed: HTTP {response.status_code}"
                return False
        except Exception as e:
            logger.error(f"CrowdStrike auth error: {e}")
            self.last_error = f"authentication error: {e}"
            return False

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to CrowdStrike API."""
        try:
            if not self._ensure_authenticated():
                return False, "Authentication failed"

            response = self.session.get(
                f"{self.base_url}/sensors/queries/sensors/v1",
                params={"limit": 1},
                timeout=30,
            )

            if response.status_code == 200:
                return True, "Connection successful"
            return False, f"API error: {response.status_code}"
        except Exception as e:
            return False, str(e)

    def _query_detection_ids(
        self, filter_query: Optional[str], limit: Optional[int]
    ) -> Optional[List[str]]:
        """Page the ID query until ``limit`` IDs (``None``: the whole window).

        Returns ``None`` on error (see ``last_error``).
        """
        ids: List[str] = []
        while limit is None or len(ids) < limit:
            page = (
                _ID_PAGE_MAX if limit is None else min(limit - len(ids), _ID_PAGE_MAX)
            )
            params: Dict[str, Any] = {"limit": page, "offset": len(ids)}
            if filter_query:
                params["filter"] = filter_query

            response = self.session.get(
                f"{self.base_url}/detects/queries/detects/v1", params=params, timeout=30
            )
            if response.status_code != 200:
                logger.error(f"Failed to query detections: {response.status_code}")
                self.last_error = f"detections query: HTTP {response.status_code}"
                return None

            data = response.json()
            resources = data.get("resources", [])
            ids.extend(resources)
            total = ((data.get("meta") or {}).get("pagination") or {}).get("total")
            if not resources or len(resources) < page or (total and len(ids) >= total):
                break
        return ids

    def get_detections(
        self, filter_query: Optional[str] = None, limit: Optional[int] = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get detections from CrowdStrike.

        Every ID the query returns is summarised, in chunks of at most 1000.

        Args:
            filter_query: FQL filter string (e.g., "created_timestamp:>='2024-01-01'")
            limit: Maximum number of detections to return, or ``None`` for
                every detection matching the filter. The ID query has no
                documented sort on ``created_timestamp``, so a caller that
                needs the oldest N must read the whole window and sort itself.

        Returns:
            List of detection details or None on error (see ``last_error``)
        """
        self.last_error = None
        try:
            if not self._ensure_authenticated():
                return None

            detection_ids = self._query_detection_ids(filter_query, limit)
            if detection_ids is None:
                return None

            details: List[Dict[str, Any]] = []
            for i in range(0, len(detection_ids), _SUMMARIES_MAX):
                detail_response = self.session.post(
                    f"{self.base_url}/detects/entities/summaries/GET/v1",
                    json={"ids": detection_ids[i : i + _SUMMARIES_MAX]},
                    timeout=30,
                )

                if detail_response.status_code != 200:
                    logger.error(
                        f"Failed to get detection details: {detail_response.status_code}"
                    )
                    self.last_error = (
                        f"detection details: HTTP {detail_response.status_code}"
                    )
                    return None

                details.extend(detail_response.json().get("resources", []))
            return details

        except Exception as e:
            logger.error(f"Error getting detections: {e}")
            self.last_error = str(e)
            return None

    def lift_containment(self, host_id: str) -> Dict[str, Any]:
        """Remove containment from a host."""
        try:
            if not self._ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}

            response = self.session.post(
                f"{self.base_url}/devices/entities/devices-actions/v2",
                params={"action_name": "lift_containment"},
                json={"ids": [host_id]},
                timeout=30,
            )

            if response.status_code == 202:
                return {
                    "success": True,
                    "host_id": host_id,
                    "action": "containment_lifted",
                }
            return {"success": False, "error": f"API error: {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
