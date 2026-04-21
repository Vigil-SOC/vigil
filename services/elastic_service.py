"""Elastic Security / Elasticsearch API service for SIEM integration."""

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


class ElasticService:
    """Service for interacting with Elasticsearch and Kibana Security APIs."""

    def __init__(
        self,
        elasticsearch_url: str,
        kibana_url: Optional[str] = None,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        index_pattern: str = ".alerts-security.alerts-default",
    ):
        self.elasticsearch_url = elasticsearch_url.rstrip("/")
        self.kibana_url = (kibana_url or "").rstrip("/") or None
        self.api_key = api_key
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.index_pattern = index_pattern

        self._es_client: Optional[httpx.AsyncClient] = None
        self._kibana_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def _build_es_client(self) -> httpx.AsyncClient:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        auth = None
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        elif self.username and self.password:
            auth = httpx.BasicAuth(self.username, self.password)
        return httpx.AsyncClient(
            base_url=self.elasticsearch_url,
            headers=headers,
            auth=auth,
            verify=self.verify_ssl,
            timeout=30.0,
        )

    def _build_kibana_client(self) -> httpx.AsyncClient:
        if not self.kibana_url:
            raise ValueError(
                "kibana_url is required for Kibana Security API calls"
            )
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "kbn-xsrf": "true",
        }
        auth = None
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        elif self.username and self.password:
            auth = httpx.BasicAuth(self.username, self.password)
        return httpx.AsyncClient(
            base_url=self.kibana_url,
            headers=headers,
            auth=auth,
            verify=self.verify_ssl,
            timeout=30.0,
        )

    @property
    def es_client(self) -> httpx.AsyncClient:
        if self._es_client is None or self._es_client.is_closed:
            self._es_client = self._build_es_client()
        return self._es_client

    @property
    def kibana_client(self) -> httpx.AsyncClient:
        if self._kibana_client is None or self._kibana_client.is_closed:
            self._kibana_client = self._build_kibana_client()
        return self._kibana_client

    async def close(self) -> None:
        if self._es_client and not self._es_client.is_closed:
            await self._es_client.aclose()
        if self._kibana_client and not self._kibana_client.is_closed:
            await self._kibana_client.aclose()

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Tuple[bool, str]:
        """Test connectivity to Elasticsearch (and Kibana if configured)."""
        try:
            resp = await self.es_client.get("/")
            resp.raise_for_status()
            info = resp.json()
            version = info.get("version", {}).get("number", "unknown")
            cluster = info.get("cluster_name", "unknown")
            msg = f"Connected to Elasticsearch {version} (cluster: {cluster})"

            if self.kibana_url:
                kb_resp = await self.kibana_client.get("/api/status")
                kb_resp.raise_for_status()
                kb_info = kb_resp.json()
                kb_version = kb_info.get("version", {}).get("number", "unknown")
                msg += f"; Kibana {kb_version}"

            logger.info(msg)
            return True, msg

        except httpx.HTTPStatusError as exc:
            err = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            logger.error(f"Elastic connection test failed: {err}")
            return False, err
        except Exception as exc:
            logger.error(f"Elastic connection test error: {exc}")
            return False, str(exc)

    # ------------------------------------------------------------------
    # Elasticsearch search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: Dict[str, Any],
        index: Optional[str] = None,
        size: int = 100,
        sort: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run an Elasticsearch query and return the raw response body."""
        target = index or self.index_pattern
        body: Dict[str, Any] = {"query": query, "size": size}
        if sort:
            body["sort"] = sort
        try:
            resp = await self.es_client.post(
                f"/{target}/_search", json=body
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Elasticsearch search error: {exc}")
            return None

    async def search_by_ip(
        self, ip: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [{"multi_match": {"query": ip, "fields": ["*"]}}],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{hours}h"}}}
                    ],
                }
            },
            index=index,
        )

    async def search_by_hash(
        self, file_hash: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [
                        {"multi_match": {"query": file_hash, "fields": ["*"]}}
                    ],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{hours}h"}}}
                    ],
                }
            },
            index=index,
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
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{hours}h"}}}
                    ],
                }
            },
            index=index,
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
                                ],
                            }
                        }
                    ],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{hours}h"}}}
                    ],
                }
            },
            index=index,
        )

    async def get_indices(self) -> Optional[List[str]]:
        """List available indices."""
        try:
            resp = await self.es_client.get("/_cat/indices?format=json")
            resp.raise_for_status()
            return [idx["index"] for idx in resp.json() if "index" in idx]
        except Exception as exc:
            logger.error(f"Error listing indices: {exc}")
            return None

    # ------------------------------------------------------------------
    # Kibana Security — Detections (alerts)
    # ------------------------------------------------------------------

    async def fetch_detection_alerts(
        self,
        query: Optional[Dict[str, Any]] = None,
        size: int = 100,
        sort_field: str = "@timestamp",
        sort_order: str = "desc",
    ) -> Optional[Dict[str, Any]]:
        """Fetch detection alerts via the Kibana Detections API."""
        body: Dict[str, Any] = {
            "query": query or {"match_all": {}},
            "size": size,
            "sort": [{sort_field: {"order": sort_order}}],
        }
        try:
            resp = await self.kibana_client.post(
                "/api/detection_engine/signals/search", json=body
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Error fetching detection alerts: {exc}")
            return None

    async def update_alert_status(
        self,
        signal_ids: List[str],
        status: str,
    ) -> bool:
        """Update alert status (open, acknowledged, closed)."""
        body: Dict[str, Any] = {
            "signal_ids": signal_ids,
            "status": status,
        }
        try:
            resp = await self.kibana_client.post(
                "/api/detection_engine/signals/status", json=body
            )
            resp.raise_for_status()
            logger.info(
                f"Updated {len(signal_ids)} alerts to status '{status}'"
            )
            return True
        except Exception as exc:
            logger.error(f"Error updating alert status: {exc}")
            return False

    # ------------------------------------------------------------------
    # Kibana Security — Cases
    # ------------------------------------------------------------------

    async def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        """Get a Kibana Security case by ID."""
        try:
            resp = await self.kibana_client.get(
                f"/api/cases/{case_id}"
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Error fetching case {case_id}: {exc}")
            return None

    async def update_case_status(
        self,
        case_id: str,
        status: str,
        version: str,
    ) -> bool:
        """Update a Kibana Security case status.

        Args:
            case_id: The Kibana case ID.
            status: One of 'open', 'in-progress', 'closed'.
            version: The case version string (required for optimistic concurrency).
        """
        body = {
            "cases": [
                {"id": case_id, "version": version, "status": status}
            ]
        }
        try:
            resp = await self.kibana_client.patch(
                "/api/cases", json=body
            )
            resp.raise_for_status()
            logger.info(f"Updated case {case_id} to status '{status}'")
            return True
        except Exception as exc:
            logger.error(f"Error updating case {case_id}: {exc}")
            return False
