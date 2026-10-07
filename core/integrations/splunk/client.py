"""Splunk API service for data enrichment."""

import logging
import time
from typing import Dict, List, Optional

import httpx

from core.integrations._base.tls import tls_verify

# urllib3.disable_warnings() used to live here to silence
# InsecureRequestWarning; httpx doesn't use urllib3 and emits no such
# warning.

logger = logging.getLogger(__name__)

# requests defaulted to no timeout and no call site passed one, so every
# request here could hang forever. read is generous because a results fetch
# can return max_count events.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)

# requests followed redirects by default; httpx does not.
_FOLLOW_REDIRECTS = True

# httpx.InvalidURL sits outside the httpx.HTTPError tree, but requests
# folded both into RequestException.
_HTTP_ERRORS = (httpx.HTTPError, httpx.InvalidURL)


# The REST API needs a leading command, but adding one to a query that has it makes
# "search" a keyword filter that silently narrows, and breaks tstats outright.
def _as_search(query: str) -> str:
    stripped = query.strip()
    leading = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if leading == "search" or stripped.startswith("|"):
        return stripped
    return f"search {stripped}"


class SplunkService:
    """Service for interacting with Splunk API."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        ca_cert_path: Optional[str] = None,
    ):
        """
        Initialize Splunk service.

        Args:
            server_url: Splunk server URL (e.g., "https://splunk.example.com:8089")
            username: Username for authentication
            password: Password for authentication
            verify_ssl: Whether to verify SSL certificates (default: True)
            ca_cert_path: PEM trusted for this client only, instead of the
                default store (ignored when verify_ssl is False)
        """
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.ca_cert_path = ca_cert_path or None
        self._session: Optional[httpx.Client] = None
        self.session_key: Optional[str] = None

    @property
    def session(self) -> httpx.Client:
        # Built on first use so an unusable CA path fails the call that needs
        # it, naming the file, instead of the constructor ("not configured").
        # verify/timeout are constructor-only on httpx.Client.
        if self._session is None:
            self._session = httpx.Client(
                verify=tls_verify(self.verify_ssl, self.ca_cert_path),
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=_FOLLOW_REDIRECTS,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        return self._session

    def authenticate(self) -> bool:
        """
        Authenticate with Splunk server and get session key.

        Returns:
            True if authentication successful, False otherwise.
        """
        session = self.session  # an unusable CA path raises here, not below
        try:
            auth_url = f"{self.server_url}/services/auth/login"
            data = {
                "username": self.username,
                "password": self.password,
                "output_mode": "json",
            }

            response = session.post(auth_url, data=data)

            if response.status_code == 200:
                result = response.json()
                self.session_key = result.get("sessionKey")
                if self.session_key:
                    session.headers.update(
                        {"Authorization": f"Splunk {self.session_key}"}
                    )
                    logger.info(
                        f"Successfully authenticated to Splunk as {self.username}"
                    )
                    return True
                else:
                    logger.error("No session key returned from Splunk")
                    return False
            else:
                logger.error(f"Authentication failed: HTTP {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Error during authentication: {e}")
            return False

    def _drop_session_key(self) -> None:
        # Drop the header too: the login POST shares this client, and a
        # stale Authorization would ride along with the new credentials.
        self.session_key = None
        self.session.headers.pop("Authorization", None)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """One authenticated call. A 401 logs in once and retries this call.

        The retry is per request: a 401 while polling a job re-sends that
        GET, not the job POST. A second 401, or a failed login, is returned
        as-is. ``authenticate`` posts the login itself and is not retried.
        """
        response = self.session.request(method, url, **kwargs)
        if response.status_code != 401:
            return response
        # Clear first so a failed login leaves the key unset and the next
        # call tries again, instead of keeping the rejected one.
        self._drop_session_key()
        logger.info("Splunk session key expired; re-authenticating")
        if not self.authenticate():
            return response
        return self.session.request(method, url, **kwargs)

    def test_connection(self) -> tuple[bool, str]:
        """
        Test connection to Splunk server.

        Returns:
            Tuple of (success, message)
        """
        try:
            if not self.authenticate():
                return False, "Authentication failed"

            # Try to get server info
            response = self._request(
                "GET",
                f"{self.server_url}/services/server/info",
                params={"output_mode": "json"},
            )

            if response.status_code == 200:
                return True, "Connection successful"
            else:
                return False, f"Connection failed: HTTP {response.status_code}"

        except (*_HTTP_ERRORS, OSError) as e:
            return False, f"Connection error: {str(e)}"

    def search(
        self,
        query: str,
        earliest_time: str = "-24h",
        latest_time: str = "now",
        max_count: int = 1000,
    ) -> Optional[List[Dict]]:
        """
        Execute a search query in Splunk.

        Blocking: this polls the search job with time.sleep and can take up
        to ~60s. That is fine on a worker thread, but async callers must go
        through asyncio.to_thread — never await-free on the event loop.

        Args:
            query: SPL (Splunk Processing Language) query
            earliest_time: Earliest time for search (default: -24h)
            latest_time: Latest time for search (default: now)
            max_count: Maximum number of results to return

        Returns:
            List of result dictionaries, or None if error
        """
        session = self.session
        try:
            if not self.session_key:
                if not self.authenticate():
                    return None

            # Create search job
            search_url = f"{self.server_url}/services/search/jobs"
            search_data = {
                "search": _as_search(query),
                "earliest_time": earliest_time,
                "latest_time": latest_time,
                "output_mode": "json",
            }

            response = self._request("POST", search_url, data=search_data)

            if response.status_code not in [200, 201]:
                logger.error(
                    f"Failed to create search job: {response.status_code} - {response.text}"
                )
                return None

            job_data = response.json()
            sid = job_data.get("sid")

            if not sid:
                logger.error("No search ID returned")
                return None

            logger.info(f"Created search job: {sid}")

            # Poll for job completion
            job_url = f"{self.server_url}/services/search/jobs/{sid}"
            max_attempts = 60  # 60 attempts with 1 second wait = 1 minute max

            for attempt in range(max_attempts):
                status_response = self._request(
                    "GET", job_url, params={"output_mode": "json"}
                )

                if status_response.status_code == 200:
                    job_status = status_response.json()
                    entry = job_status.get("entry", [{}])[0]
                    content = entry.get("content", {})

                    is_done = content.get("isDone", False)

                    if is_done:
                        # Get results
                        results_url = f"{job_url}/results"
                        results_response = self._request(
                            "GET",
                            results_url,
                            params={"output_mode": "json", "count": max_count},
                        )

                        if results_response.status_code == 200:
                            results_data = results_response.json()
                            results = results_data.get("results", [])
                            logger.info(f"Search completed with {len(results)} results")

                            # Cleanup ignores status. A 401 here must not
                            # throw away results already in hand; the next
                            # call re-authenticates on its own 401.
                            session.delete(job_url)

                            return results
                        else:
                            logger.error(
                                f"Failed to get results: {results_response.status_code}"
                            )
                            return None

                    time.sleep(1)
                else:
                    logger.error(
                        f"Failed to check job status: {status_response.status_code}"
                    )
                    return None

            logger.error("Search job timed out")
            # Try to cancel the job. Status is ignored, same as a
            # successful search's cleanup.
            session.delete(job_url)
            return None

        except Exception as e:
            logger.error(f"Error executing search: {e}")
            return None

    @staticmethod
    def _earliest_from_hours(hours: int) -> str:
        """Turn a look-back window in hours into a valid Splunk earliest_time.

        Callers pass a very large ``hours`` to mean "all time" (the tool's
        all-time sentinel is 876000h). Splunk rejects such an offset as
        ``Invalid earliest_time``, so map an absurd or non-positive window to
        Splunk's all-time epoch ``"0"`` instead.
        """
        if not hours or hours <= 0 or hours >= 87600:  # ~10 years -> all time
            return "0"
        return f"-{hours}h"

    def search_by_ip(self, ip_address: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to an IP address.

        Args:
            ip_address: IP address to search for
            hours: Number of hours to look back (default: 24)

        Returns:
            List of events or None
        """
        query = f'search index=* "{ip_address}" | head 1000'
        return self.search(query, earliest_time=self._earliest_from_hours(hours))

    def search_by_hostname(
        self, hostname: str, hours: int = 24
    ) -> Optional[List[Dict]]:
        """
        Search for events related to a hostname.

        Args:
            hostname: Hostname to search for
            hours: Number of hours to look back (default: 24)

        Returns:
            List of events or None
        """
        query = (
            f'search index=* (host="{hostname}" OR hostname="{hostname}" '
            f'OR dest="{hostname}") | head 1000'
        )
        return self.search(query, earliest_time=self._earliest_from_hours(hours))
