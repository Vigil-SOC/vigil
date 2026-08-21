"""Splunk API service for data enrichment."""

import logging
from typing import Dict, List, Optional

import httpx

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
        self, server_url: str, username: str, password: str, verify_ssl: bool = False
    ):
        """
        Initialize Splunk service.

        Args:
            server_url: Splunk server URL (e.g., "https://splunk.example.com:8089")
            username: Username for authentication
            password: Password for authentication
            verify_ssl: Whether to verify SSL certificates (default: False)
        """
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        # verify/timeout are constructor-only on httpx.Client; requests
        # allowed session.verify to be assigned afterwards.
        self.session = httpx.Client(
            verify=verify_ssl,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=_FOLLOW_REDIRECTS,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        self.session_key: Optional[str] = None

    def authenticate(self) -> bool:
        """
        Authenticate with Splunk server and get session key.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            auth_url = f"{self.server_url}/services/auth/login"
            data = {
                "username": self.username,
                "password": self.password,
                "output_mode": "json",
            }

            response = self.session.post(auth_url, data=data)

            if response.status_code == 200:
                result = response.json()
                self.session_key = result.get("sessionKey")
                if self.session_key:
                    self.session.headers.update(
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
            response = self.session.get(
                f"{self.server_url}/services/server/info",
                params={"output_mode": "json"},
            )

            if response.status_code == 200:
                return True, "Connection successful"
            else:
                return False, f"Connection failed: HTTP {response.status_code}"

        except _HTTP_ERRORS as e:
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

            response = self.session.post(search_url, data=search_data)

            if response.status_code not in [200, 201]:
                logger.error(
                    "Failed to create search job: "
                    f"{response.status_code} - {response.text}"
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
                status_response = self.session.get(
                    job_url, params={"output_mode": "json"}
                )

                if status_response.status_code == 200:
                    job_status = status_response.json()
                    entry = job_status.get("entry", [{}])[0]
                    content = entry.get("content", {})

                    is_done = content.get("isDone", False)

                    if is_done:
                        # Get results
                        results_url = f"{job_url}/results"
                        results_response = self.session.get(
                            results_url,
                            params={"output_mode": "json", "count": max_count},
                        )

                        if results_response.status_code == 200:
                            results_data = results_response.json()
                            results = results_data.get("results", [])
                            logger.info(f"Search completed with {len(results)} results")

                            # Clean up job
                            self.session.delete(job_url)

                            return results
                        else:
                            logger.error(
                                f"Failed to get results: {results_response.status_code}"
                            )
                            return None

                    # Wait before next poll
                    import time

                    time.sleep(1)
                else:
                    logger.error(
                        f"Failed to check job status: {status_response.status_code}"
                    )
                    return None

            logger.error("Search job timed out")
            # Try to cancel the job
            self.session.delete(job_url)
            return None

        except Exception as e:
            logger.error(f"Error executing search: {e}")
            return None

    def search_by_ip(self, ip_address: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to an IP address.

        Args:
            ip_address: IP address to search for
            hours: Number of hours to look back (default: 24)

        Returns:
            List of events or None
        """
        query = f'"{ip_address}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")

    def search_by_hash(self, file_hash: str, hours: int = 24) -> Optional[List[Dict]]:
        """
        Search for events related to a file hash.

        Args:
            file_hash: File hash (MD5, SHA1, or SHA256) to search for
            hours: Number of hours to look back (default: 24)

        Returns:
            List of events or None
        """
        query = f'"{file_hash}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")

    def search_by_username(
        self, username: str, hours: int = 24
    ) -> Optional[List[Dict]]:
        """
        Search for events related to a username.

        Args:
            username: Username to search for
            hours: Number of hours to look back (default: 24)

        Returns:
            List of events or None
        """
        query = f'user="{username}" OR username="{username}" OR account="{username}" | head 1000'
        return self.search(query, earliest_time=f"-{hours}h")

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
            f'host="{hostname}" OR hostname="{hostname}" OR dest="{hostname}" '
            "| head 1000"
        )
        return self.search(query, earliest_time=f"-{hours}h")
