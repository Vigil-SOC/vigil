"""CrowdStrike Falcon API service for alert polling and host management."""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from core.time import utcnow

logger = logging.getLogger(__name__)

# Default for every request; each call site also passes self.timeout.
DEFAULT_TIMEOUT = 30.0

# Falcon caps: IDs per alerts-query page, and composite IDs per entities request.
_ID_PAGE_MAX = 10000
_ALERTS_MAX = 1000

# requests followed redirects by default; httpx does not.
_FOLLOW_REDIRECTS = True


class CrowdStrikeService:
    """Service for interacting with CrowdStrike Falcon API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.crowdstrike.com",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        # Detail of the last failed get_alerts call (e.g. "HTTP 500 ..."),
        # so callers can report why it returned None.
        self.last_error: Optional[str] = None
        # HTTP status of that failure, when the response had one. Not parsed
        # back out of last_error.
        self.last_status: Optional[int] = None
        self.timeout = timeout
        self.session = httpx.Client(
            timeout=timeout,
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
                timeout=self.timeout,
            )

            self.last_status = None
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
                self.last_status = response.status_code
                return False
        except Exception as e:
            logger.error(f"CrowdStrike auth error: {e}")
            self.last_error = f"authentication error: {e}"
            self.last_status = None
            return False

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to CrowdStrike API."""
        try:
            if not self._ensure_authenticated():
                return False, "Authentication failed"

            response = self.session.get(
                f"{self.base_url}/sensors/queries/sensors/v1",
                params={"limit": 1},
                timeout=self.timeout,
            )

            if response.status_code == 200:
                return True, "Connection successful"
            return False, f"API error: {response.status_code}"
        except Exception as e:
            return False, str(e)

    def _query_alert_ids(
        self,
        filter_query: Optional[str],
        limit: Optional[int],
        sort: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Page the alerts ID query until ``limit`` IDs (``None``: the whole window).

        Returns ``None`` on error (see ``last_error``). An empty ``resources``
        list with HTTP 200 is an empty window, not an error.
        """
        ids: List[str] = []
        while limit is None or len(ids) < limit:
            page = (
                _ID_PAGE_MAX if limit is None else min(limit - len(ids), _ID_PAGE_MAX)
            )
            params: Dict[str, Any] = {"limit": page, "offset": len(ids)}
            if filter_query:
                params["filter"] = filter_query
            if sort:
                params["sort"] = sort

            response = self.session.get(
                f"{self.base_url}/alerts/queries/alerts/v2",
                params=params,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                logger.error(f"Failed to query alerts: {response.status_code}")
                self.last_error = f"alerts query: HTTP {response.status_code}"
                self.last_status = response.status_code
                return None

            data = response.json()
            resources = data.get("resources", [])
            ids.extend(resources)
            total = ((data.get("meta") or {}).get("pagination") or {}).get("total")
            if not resources or len(resources) < page or (total and len(ids) >= total):
                break
        if limit is not None and len(ids) > limit:
            return ids[:limit]
        return ids

    def get_alerts(
        self,
        filter_query: Optional[str] = None,
        limit: Optional[int] = 100,
        sort: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Get alerts from CrowdStrike.

        Every composite ID the query returns is fetched, in chunks of at most
        1000. ``sort`` is the query's sort (for example
        ``created_timestamp|asc``). The default order is newest first, so a
        caller that needs the oldest page passes that sort and a ``limit``.

        Returns the alert resources, or ``None`` on error (see ``last_error``).
        """
        self.last_error = None
        self.last_status = None
        try:
            if not self._ensure_authenticated():
                return None

            composite_ids = self._query_alert_ids(filter_query, limit, sort)
            if composite_ids is None:
                return None

            details: List[Dict[str, Any]] = []
            for i in range(0, len(composite_ids), _ALERTS_MAX):
                detail_response = self.session.post(
                    f"{self.base_url}/alerts/entities/alerts/v2",
                    json={"composite_ids": composite_ids[i : i + _ALERTS_MAX]},
                    timeout=self.timeout,
                )

                if detail_response.status_code != 200:
                    logger.error(
                        f"Failed to get alert details: {detail_response.status_code}"
                    )
                    self.last_error = (
                        f"alert details: HTTP {detail_response.status_code}"
                    )
                    self.last_status = detail_response.status_code
                    return None

                details.extend(detail_response.json().get("resources", []))
            return details

        except Exception as e:
            logger.error(f"Error getting alerts: {e}")
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
                timeout=self.timeout,
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
