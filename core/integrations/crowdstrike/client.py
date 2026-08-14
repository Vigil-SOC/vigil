"""CrowdStrike Falcon API service for detection polling and host management."""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Client-level floor; every call site also passes timeout=30 explicitly.
DEFAULT_TIMEOUT = 30.0

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
        self.session = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=_FOLLOW_REDIRECTS,
        )

    def _ensure_authenticated(self) -> bool:
        """Ensure we have a valid access token."""
        if self.access_token and self.token_expiry:
            if datetime.utcnow() < self.token_expiry:
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
                self.token_expiry = datetime.utcnow() + timedelta(
                    seconds=expires_in - 60
                )
                self.session.headers.update(
                    {"Authorization": f"Bearer {self.access_token}"}
                )
                logger.info("CrowdStrike authentication successful")
                return True
            else:
                logger.error(f"CrowdStrike auth failed: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"CrowdStrike auth error: {e}")
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

    def get_detections(
        self, filter_query: Optional[str] = None, limit: int = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get detections from CrowdStrike.

        Args:
            filter_query: FQL filter string (e.g., "created_timestamp:>='2024-01-01'")
            limit: Maximum number of detections to return

        Returns:
            List of detection details or None on error
        """
        try:
            if not self._ensure_authenticated():
                return None

            # Get detection IDs
            params = {"limit": limit}
            if filter_query:
                params["filter"] = filter_query

            response = self.session.get(
                f"{self.base_url}/detects/queries/detects/v1", params=params, timeout=30
            )

            if response.status_code != 200:
                logger.error(f"Failed to query detections: {response.status_code}")
                return None

            data = response.json()
            detection_ids = data.get("resources", [])

            if not detection_ids:
                return []

            # Get detection details
            detail_response = self.session.post(
                f"{self.base_url}/detects/entities/summaries/GET/v1",
                json={"ids": detection_ids[:100]},
                timeout=30,
            )

            if detail_response.status_code != 200:
                logger.error(
                    f"Failed to get detection details: {detail_response.status_code}"
                )
                return None

            details = detail_response.json()
            return details.get("resources", [])

        except Exception as e:
            logger.error(f"Error getting detections: {e}")
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
