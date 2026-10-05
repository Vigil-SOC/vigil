"""OpenSearch API service for SIEM integration.

OpenSearch is a fork of Elasticsearch 7.10, so the search surface used here
(``_search``, ``_cat/indices``, the query DSL) is the one the Elastic client
uses. The differences are in auth and in the SIEM layer: open-source
OpenSearch has no API-key auth (its security plugin authenticates users with
basic auth), and there is no Kibana detection engine — detections come from
the Security Analytics plugin, whose findings are ordinary documents in
``.opensearch-sap-*-findings-*`` indices and are read with plain searches.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.integrations._base.tls import tls_verify

logger = logging.getLogger(__name__)

# Where log and IOC searches look unless told otherwise: every index but the
# hidden system ones. index_pattern is the findings pattern, whose documents
# are identifiers only and carry no ``@timestamp``.
LOG_INDICES = "*,-.*"


class OpenSearchService:
    """Service for interacting with OpenSearch and OpenSearch Dashboards."""

    def __init__(
        self,
        opensearch_url: str,
        dashboards_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        index_pattern: str = ".opensearch-sap-*-findings-*",
        ca_cert_path: Optional[str] = None,
    ):
        self.opensearch_url = opensearch_url.rstrip("/")
        self.dashboards_url = (dashboards_url or "").rstrip("/") or None
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.index_pattern = index_pattern
        self.ca_cert_path = ca_cert_path or None

        self._client: Optional[httpx.AsyncClient] = None
        self._dashboards_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _auth(self) -> Optional[httpx.BasicAuth]:
        if self.username and self.password:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.opensearch_url,
            headers={"Content-Type": "application/json"},
            auth=self._auth(),
            verify=tls_verify(self.verify_ssl, self.ca_cert_path),
            timeout=30.0,
        )

    def _build_dashboards_client(self) -> httpx.AsyncClient:
        if not self.dashboards_url:
            raise ValueError(
                "dashboards_url is required for OpenSearch Dashboards API calls"
            )
        return httpx.AsyncClient(
            base_url=self.dashboards_url,
            headers={"Content-Type": "application/json", "osd-xsrf": "true"},
            auth=self._auth(),
            verify=tls_verify(self.verify_ssl, self.ca_cert_path),
            timeout=30.0,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    @property
    def dashboards_client(self) -> httpx.AsyncClient:
        if self._dashboards_client is None or self._dashboards_client.is_closed:
            self._dashboards_client = self._build_dashboards_client()
        return self._dashboards_client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._dashboards_client and not self._dashboards_client.is_closed:
            await self._dashboards_client.aclose()

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Tuple[bool, str]:
        """Test connectivity to OpenSearch (and Dashboards if configured)."""
        try:
            resp = await self.client.get("/")
            resp.raise_for_status()
            info = resp.json()
            version = info.get("version", {}).get("number", "unknown")
            cluster = info.get("cluster_name", "unknown")
            msg = f"Connected to OpenSearch {version} (cluster: {cluster})"

            if self.dashboards_url:
                db_resp = await self.dashboards_client.get("/api/status")
                db_resp.raise_for_status()
                db_info = db_resp.json()
                db_version = db_info.get("version", {}).get("number", "unknown")
                msg += f"; OpenSearch Dashboards {db_version}"

            logger.info(msg)
            return True, msg

        except httpx.HTTPStatusError as exc:
            err = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.error(f"OpenSearch connection test failed: {err}")
            return False, err
        except Exception as exc:
            logger.error(f"OpenSearch connection test error: {exc}")
            return False, str(exc)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: Dict[str, Any],
        index: Optional[str] = None,
        size: int = 100,
        sort: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run an OpenSearch query and return the raw response body.

        A failed request returns None, but a client that cannot be built (an
        unusable CA path) raises, so the caller reports why.
        """
        target = index or self.index_pattern
        body: Dict[str, Any] = {"query": query, "size": size}
        if sort:
            body["sort"] = sort
        client = self.client
        try:
            resp = await client.post(f"/{target}/_search", json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"OpenSearch search error: {exc}")
            return None

    async def search_by_ip(
        self, ip: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [{"multi_match": {"query": ip, "fields": ["*"]}}],
                    "filter": [{"range": {"@timestamp": {"gte": f"now-{hours}h"}}}],
                }
            },
            index=index or LOG_INDICES,
        )

    async def search_by_hash(
        self, file_hash: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [{"multi_match": {"query": file_hash, "fields": ["*"]}}],
                    "filter": [{"range": {"@timestamp": {"gte": f"now-{hours}h"}}}],
                }
            },
            index=index or LOG_INDICES,
        )

    async def search_by_username(
        self, username: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": username,
                                "fields": [
                                    "user.name",
                                    "user.id",
                                    "winlog.event_data.TargetUserName",
                                ],
                            }
                        }
                    ],
                    "filter": [{"range": {"@timestamp": {"gte": f"now-{hours}h"}}}],
                }
            },
            index=index or LOG_INDICES,
        )

    async def search_by_hostname(
        self, hostname: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": hostname,
                                "fields": [
                                    "host.name",
                                    "host.hostname",
                                    "agent.hostname",
                                    "agent.name",
                                ],
                            }
                        }
                    ],
                    "filter": [{"range": {"@timestamp": {"gte": f"now-{hours}h"}}}],
                }
            },
            index=index or LOG_INDICES,
        )

    async def get_indices(self) -> Optional[List[str]]:
        """List available indices."""
        client = self.client
        try:
            resp = await client.get("/_cat/indices?format=json")
            resp.raise_for_status()
            return [idx["index"] for idx in resp.json() if "index" in idx]
        except Exception as exc:
            logger.error(f"Error listing indices: {exc}")
            return None
