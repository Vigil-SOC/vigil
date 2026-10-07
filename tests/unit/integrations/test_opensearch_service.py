"""Unit tests for core/integrations/opensearch/client.py."""

import json

import httpx
import pytest
import respx

from core.integrations.opensearch.client import OpenSearchService

OS_URL = "https://os.test:9200"
DASHBOARDS_URL = "https://dashboards.test:5601"


def _multi_match_fields(route) -> set:
    body = json.loads(route.calls.last.request.content)
    return set(body["query"]["bool"]["must"][0]["multi_match"]["fields"])


@pytest.fixture
def service():
    return OpenSearchService(
        opensearch_url=OS_URL,
        dashboards_url=DASHBOARDS_URL,
        username="admin",
        password="test-password",
        verify_ssl=False,
    )


@pytest.fixture
def service_no_auth():
    return OpenSearchService(opensearch_url=OS_URL, verify_ssl=False)


# ------------------------------------------------------------------
# Client construction
# ------------------------------------------------------------------


class TestClientConstruction:

    def test_basic_auth_is_used(self, service):
        client = service._build_client()
        assert client.auth is not None
        # Basic auth only: open-source OpenSearch has no ApiKey scheme.
        assert "Authorization" not in client.headers

    def test_no_auth_without_credentials(self, service_no_auth):
        client = service_no_auth._build_client()
        assert client.auth is None

    def test_default_index_pattern_is_security_analytics_findings(self, service):
        assert service.index_pattern == ".opensearch-sap-*-findings-*"

    def test_dashboards_client_requires_url(self, service_no_auth):
        with pytest.raises(ValueError):
            service_no_auth._build_dashboards_client()

    def test_dashboards_client_sends_osd_xsrf(self, service):
        client = service._build_dashboards_client()
        assert client.headers["osd-xsrf"] == "true"


# ------------------------------------------------------------------
# Connection test
# ------------------------------------------------------------------


class TestConnection:

    @respx.mock
    @pytest.mark.asyncio
    async def test_connects_and_reports_versions(self, service):
        respx.get(f"{OS_URL}/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "name": "node-1",
                    "cluster_name": "opensearch-cluster",
                    "version": {"distribution": "opensearch", "number": "2.17.0"},
                },
            )
        )
        respx.get(f"{DASHBOARDS_URL}/api/status").mock(
            return_value=httpx.Response(200, json={"version": {"number": "2.17.0"}})
        )
        ok, msg = await service.test_connection()
        assert ok is True
        assert "OpenSearch 2.17.0" in msg
        assert "opensearch-cluster" in msg
        assert "OpenSearch Dashboards 2.17.0" in msg

    @respx.mock
    @pytest.mark.asyncio
    async def test_connects_without_dashboards(self, service_no_auth):
        respx.get(f"{OS_URL}/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "cluster_name": "opensearch-cluster",
                    "version": {"distribution": "opensearch", "number": "2.17.0"},
                },
            )
        )
        ok, msg = await service_no_auth.test_connection()
        assert ok is True
        assert "Dashboards" not in msg

    @respx.mock
    @pytest.mark.asyncio
    async def test_connection_failure_reports_http_status(self, service):
        respx.get(f"{OS_URL}/").mock(return_value=httpx.Response(401, text="denied"))
        ok, msg = await service.test_connection()
        assert ok is False
        assert "401" in msg


# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------


class TestSearch:

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_posts_to_configured_index_pattern(self, service):
        route = respx.post(f"{OS_URL}/.opensearch-sap-*-findings-*/_search").mock(
            return_value=httpx.Response(
                200, json={"hits": {"total": {"value": 0}, "hits": []}}
            )
        )
        data = await service.search(query={"match_all": {}}, size=5)
        assert data is not None
        body = json.loads(route.calls.last.request.content)
        assert body["size"] == 5
        assert body["query"] == {"match_all": {}}

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_returns_none_on_http_error(self, service):
        respx.post(f"{OS_URL}/.opensearch-sap-*-findings-*/_search").mock(
            return_value=httpx.Response(500, text="boom")
        )
        assert await service.search(query={"match_all": {}}) is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_by_username_fields(self, service):
        route = respx.post(f"{OS_URL}/*,-.*/_search").mock(
            return_value=httpx.Response(
                200, json={"hits": {"total": {"value": 0}, "hits": []}}
            )
        )
        await service.search_by_username("jsmith")
        fields = _multi_match_fields(route)
        assert "user.name" in fields
        assert "winlog.event_data.TargetUserName" in fields

    @respx.mock
    @pytest.mark.asyncio
    async def test_ioc_search_skips_findings_and_system_indices_by_default(
        self, service
    ):
        # Findings carry identifiers and an epoch-ms ``timestamp``, no
        # ``@timestamp``, so an IOC search over them would never match.
        route = respx.post(f"{OS_URL}/*,-.*/_search").mock(
            return_value=httpx.Response(
                200, json={"hits": {"total": {"value": 0}, "hits": []}}
            )
        )
        await service.search_by_ip("10.0.0.1")
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_ioc_search_honours_explicit_index(self, service):
        route = respx.post(f"{OS_URL}/windows-logs/_search").mock(
            return_value=httpx.Response(
                200, json={"hits": {"total": {"value": 0}, "hits": []}}
            )
        )
        await service.search_by_hostname("dc01", index="windows-logs")
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_indices(self, service):
        respx.get(f"{OS_URL}/_cat/indices?format=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"index": ".opensearch-sap-windows-findings-2026.10.04"},
                    {"index": "windows-logs"},
                    {"health": "green"},
                ],
            )
        )
        indices = await service.get_indices()
        assert indices == [
            ".opensearch-sap-windows-findings-2026.10.04",
            "windows-logs",
        ]
