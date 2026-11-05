"""Elastic Security / Elasticsearch API service for SIEM integration."""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from core.integrations._base.proxy import integration_proxy
from core.integrations._base.search_params import validate_index
from core.integrations._base.tls import tls_verify

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
        ca_cert_path: Optional[str] = None,
    ):
        self.elasticsearch_url = elasticsearch_url.rstrip("/")
        self.kibana_url = (kibana_url or "").rstrip("/") or None
        self.api_key = api_key
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.index_pattern = index_pattern
        self.ca_cert_path = ca_cert_path or None

        self._es_client: Optional[httpx.AsyncClient] = None
        self._kibana_client: Optional[httpx.AsyncClient] = None
        # The exception behind the last search/fetch that returned None, so a
        # caller that raises can chain it and the failure keeps its type.
        self.last_search_error: Optional[BaseException] = None

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
            verify=tls_verify(self.verify_ssl, self.ca_cert_path),
            proxy=integration_proxy("elastic-siem"),
            timeout=30.0,
        )

    def _build_kibana_client(self) -> httpx.AsyncClient:
        if not self.kibana_url:
            raise ValueError("kibana_url is required for Kibana Security API calls")
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
            verify=tls_verify(self.verify_ssl, self.ca_cert_path),
            proxy=integration_proxy("elastic-siem"),
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
        """Run an Elasticsearch query and return the raw response body.

        A failed request returns None, but a client that cannot be built (an
        unusable CA path) or an invalid index name raises, so the caller
        reports why.
        """
        target = validate_index(index or self.index_pattern)
        body: Dict[str, Any] = {"query": query, "size": size}
        if sort:
            body["sort"] = sort
        client = self.es_client
        self.last_search_error = None
        try:
            resp = await client.post(f"/{target}/_search", json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Elasticsearch search error: {exc}")
            self.last_search_error = exc
            return None

    async def search_by_ip(
        self, ip: str, index: Optional[str] = None, hours: int = 24
    ) -> Optional[Dict[str, Any]]:
        return await self.search(
            query={
                "bool": {
                    "must": [{"multi_match": {"query": ip, "fields": ["*"]}}],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{int(hours)}h"}}}
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
                    "must": [{"multi_match": {"query": file_hash, "fields": ["*"]}}],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{int(hours)}h"}}}
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
                                    # Wazuh indexer (wazuh-alerts-4.x-*)
                                    "data.srcuser",
                                    "data.dstuser",
                                    "data.win.eventdata.targetUserName",
                                ],
                            }
                        }
                    ],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{int(hours)}h"}}}
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
                                    # Wazuh indexer: its pipeline drops `host`
                                    "agent.name",
                                    "data.win.system.computer",
                                ],
                            }
                        }
                    ],
                    "filter": [
                        {"range": {"@timestamp": {"gte": f"now-{int(hours)}h"}}}
                    ],
                }
            },
            index=index,
        )

    async def any_event_since(
        self, index: str, timestamp_field: str, days: int
    ) -> bool:
        """Whether ``index`` holds an event in the last ``days``: one size-0 search
        that stops at the first hit. A missing index reads as no events; any
        other failure raises."""
        resp = await self.es_client.post(
            f"/{validate_index(index)}/_search",
            params={"ignore_unavailable": "true", "allow_no_indices": "true"},
            json={
                "size": 0,
                "terminate_after": 1,
                "track_total_hits": 1,
                "query": {"range": {timestamp_field: {"gte": f"now-{int(days)}d"}}},
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if (body.get("_shards") or {}).get("failed"):
            raise RuntimeError(f"shard failures: {body['_shards'].get('failures')}")
        total = (body.get("hits") or {}).get("total")
        return int(total.get("value") if isinstance(total, dict) else total or 0) > 0

    async def terms_since(
        self, index: str, fields: List[str], days: int, size: int = 10000
    ) -> Dict[str, Any]:
        """One size-0 search over the last ``days`` of ``index`` (on @timestamp)
        with a terms aggregation per field: ``{"total": hits, field: {"values":
        set, "other": docs past the size}}``. A missing index reads as no events;
        any other failure raises."""
        resp = await self.es_client.post(
            f"/{validate_index(index)}/_search",
            params={"ignore_unavailable": "true", "allow_no_indices": "true"},
            json={
                "size": 0,
                "track_total_hits": 1,
                "query": {"range": {"@timestamp": {"gte": f"now-{int(days)}d"}}},
                "aggs": {f: {"terms": {"field": f, "size": size}} for f in fields},
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if (body.get("_shards") or {}).get("failed"):
            raise RuntimeError(f"shard failures: {body['_shards'].get('failures')}")
        if body.get("timed_out"):
            raise RuntimeError("the search timed out: its buckets are partial")
        total = (body.get("hits") or {}).get("total")
        out: Dict[str, Any] = {
            "total": int(total.get("value") if isinstance(total, dict) else total or 0)
        }
        aggs = body.get("aggregations") or {}
        for f in fields:
            agg = aggs.get(f) or {}
            out[f] = {
                "values": {str(b["key"]) for b in agg.get("buckets") or []},
                "other": int(agg.get("sum_other_doc_count") or 0),
            }
        return out

    # Rule queries (the overlay runner): each returns (total hits, hits as
    # documents) and raises with Elasticsearch's own reason on a failure.

    async def _rule_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = await self.es_client.post(path, json=body)
        if resp.status_code >= 400:
            try:
                error = resp.json().get("error") or {}
            except ValueError:
                error = {}
            cause = (error.get("root_cause") or [error])[0] if error else {}
            reason = cause.get("reason") if isinstance(cause, dict) else None
            raise RuntimeError(reason or f"HTTP {resp.status_code}")
        body = resp.json()
        if (body.get("_shards") or {}).get("failed"):
            raise RuntimeError(f"shard failures: {body['_shards'].get('failures')}")
        return body

    @staticmethod
    def _total(hits: Dict[str, Any], fallback: int) -> int:
        total = hits.get("total")
        if isinstance(total, dict):
            return int(total.get("value") or 0)
        return int(total) if total is not None else fallback

    async def eql_search(
        self, index: str, query: str, time_range: Dict[str, Any], size: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """``POST /<index>/_eql/search``; a sequence comes back as one hit holding its events."""
        body = await self._rule_post(
            f"/{validate_index(index)}/_eql/search",
            {
                "query": query,
                "filter": {"range": time_range},
                "size": size,
                # The range is on the rule's timestamp field; EQL orders by it too.
                "timestamp_field": next(iter(time_range)),
            },
        )
        hits = body.get("hits") or {}
        docs = list(hits.get("events") or [])
        for seq in hits.get("sequences") or []:
            events = seq.get("events") or []
            docs.append(
                {
                    "_id": "+".join(str(e.get("_id")) for e in events),
                    "_index": ",".join(sorted({str(e.get("_index")) for e in events})),
                    "_source": {"sequence": [e.get("_source") for e in events]},
                }
            )
        return self._total(hits, len(docs)), docs

    async def esql_query(
        self, query: str, time_range: Dict[str, Any]
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """``POST /_query`` with the time range as a filter; each row becomes a document."""
        body = await self._rule_post(
            "/_query", {"query": query, "filter": {"range": time_range}}
        )
        names = [c.get("name") for c in body.get("columns") or []]
        docs = []
        for row in body.get("values") or []:
            source = dict(zip(names, row))
            docs.append(
                {
                    "_id": source.pop("_id", None),
                    "_index": source.pop("_index", None),
                    "_source": source,
                }
            )
        return len(docs), docs

    async def lucene_search(
        self, index: str, query: str, time_range: Dict[str, Any], size: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """``POST /<index>/_search`` with ``query_string``."""
        body = await self._rule_post(
            f"/{validate_index(index)}/_search",
            {
                "size": size,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "must": [{"query_string": {"query": query}}],
                        "filter": [{"range": time_range}],
                    }
                },
            },
        )
        hits = body.get("hits") or {}
        docs = list(hits.get("hits") or [])
        return self._total(hits, len(docs)), docs

    async def field_caps(self, index: str, fields: List[str]) -> set:
        """Which of ``fields`` the mapping of ``index`` has. Metadata, not a search."""
        resp = await self.es_client.get(
            f"/{validate_index(index)}/_field_caps",
            params={
                "fields": ",".join(fields),
                "ignore_unavailable": "true",
                "allow_no_indices": "true",
            },
        )
        resp.raise_for_status()
        return set(resp.json().get("fields") or {})

    async def get_data_view(self, data_view_id: str) -> Dict[str, Any]:
        """A Kibana data view (its ``title`` holds the index patterns). Read-only."""
        resp = await self.kibana_client.get(
            f"/api/data_views/data_view/{quote(data_view_id, safe='')}"
        )
        resp.raise_for_status()
        return resp.json().get("data_view") or {}

    async def get_indices(self) -> Optional[List[str]]:
        """List available indices, or None with the error in ``last_indices_error``."""
        client = self.es_client
        self.last_indices_error: Optional[str] = None
        try:
            resp = await client.get("/_cat/indices?format=json")
            resp.raise_for_status()
            return [idx["index"] for idx in resp.json() if "index" in idx]
        except Exception as exc:
            logger.error(f"Error listing indices: {exc}")
            self.last_indices_error = f"{type(exc).__name__}: {exc}"
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
        client = self.kibana_client
        self.last_search_error = None
        try:
            resp = await client.post("/api/detection_engine/signals/search", json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Error fetching detection alerts: {exc}")
            self.last_search_error = exc
            return None

    async def alerts_per_rule_per_day(
        self, rule_ids: List[str], days: int, size: int
    ) -> Dict[str, Any]:
        """Alert counts per rule_id per UTC day since ``days`` - 1 days ago midnight:
        one size-0 search through Kibana's signals route, building blocks excluded.
        Returns the ``rules`` terms aggregation; any failure raises."""
        resp = await self.kibana_client.post(
            "/api/detection_engine/signals/search",
            json={
                "size": 0,
                "track_total_hits": False,
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "range": {
                                    "@timestamp": {"gte": f"now-{int(days) - 1}d/d"}
                                }
                            }
                        ],
                        "must_not": [
                            {"exists": {"field": "kibana.alert.building_block_type"}}
                        ],
                    }
                },
                "aggs": {
                    "rules": {
                        "terms": {
                            "field": "kibana.alert.rule.rule_id",
                            "include": rule_ids,
                            "size": size,
                        },
                        "aggs": {
                            "days": {
                                "date_histogram": {
                                    "field": "@timestamp",
                                    "calendar_interval": "1d",
                                    "time_zone": "UTC",
                                    "min_doc_count": 1,
                                }
                            }
                        },
                    }
                },
            },
        )
        if resp.is_error:  # Kibana's own message says why, e.g. too many buckets
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        if (body.get("_shards") or {}).get("failed"):
            raise RuntimeError(f"shard failures: {body['_shards'].get('failures')}")
        rules = (body.get("aggregations") or {}).get("rules")
        if rules is None:
            raise RuntimeError("the response has no rules aggregation")
        return rules

    async def list_detection_rules(self, per_page: int = 100) -> List[Dict[str, Any]]:
        """Every detection engine rule, paged through ``_find``. Read-only.

        Raises unless the list is whole: as many rules as the response's
        ``total``, no ``rule_id`` twice. A caller treating a short list as
        complete would read the missing rules as deleted.
        """
        rules: List[Dict[str, Any]] = []
        total: Optional[int] = None
        page = 1
        while total is None or len(rules) < total:
            resp = await self.kibana_client.get(
                "/api/detection_engine/rules/_find",
                params={
                    "page": page,
                    "per_page": per_page,
                    # created_at ascending: a rule added mid-list lands last.
                    "sort_field": "created_at",
                    "sort_order": "asc",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or []
            if total is None:
                total = int(body.get("total") or 0)
            elif int(body.get("total") or 0) != total:
                raise RuntimeError("rule count changed while paging _find")
            if not data:
                break
            rules.extend(data)
            page += 1
        if len(rules) != total:
            raise RuntimeError(f"_find listed {len(rules)} rules of total {total}")
        ids = [rule.get("rule_id") for rule in rules]
        if None in ids or len(set(ids)) != len(ids):
            raise RuntimeError("_find listed a rule_id twice, or a rule without one")
        return rules

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
            logger.info(f"Updated {len(signal_ids)} alerts to status '{status}'")
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
            resp = await self.kibana_client.get(f"/api/cases/{case_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"Error fetching case {case_id}: {exc}")
            return None
