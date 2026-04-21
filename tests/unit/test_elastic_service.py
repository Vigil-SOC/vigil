"""Unit tests for services/elastic_service.py."""

import pytest
import httpx
import respx

from services.elastic_service import ElasticService


ES_URL = "https://es.test:9200"
KIBANA_URL = "https://kibana.test:5601"


@pytest.fixture
def service():
    return ElasticService(
        elasticsearch_url=ES_URL,
        kibana_url=KIBANA_URL,
        api_key="test-api-key",
        verify_ssl=False,
    )


@pytest.fixture
def service_basic_auth():
    return ElasticService(
        elasticsearch_url=ES_URL,
        kibana_url=KIBANA_URL,
        username="elastic",
        password="secret",
        verify_ssl=False,
    )


# ------------------------------------------------------------------
# Client construction
# ------------------------------------------------------------------

class TestClientConstruction:

    def test_api_key_auth_header(self, service):
        client = service._build_es_client()
        assert client.headers["Authorization"] == "ApiKey test-api-key"

    def test_basic_auth(self, service_basic_auth):
        client = service_basic_auth._build_es_client()
        assert client._auth is not None

    def test_kibana_client_requires_url(self):
        svc = ElasticService(elasticsearch_url=ES_URL)
        with pytest.raises(ValueError, match="kibana_url is required"):
            svc._build_kibana_client()

    def test_kibana_client_has_kbn_xsrf(self, service):
        client = service._build_kibana_client()
        assert client.headers["kbn-xsrf"] == "true"


# ------------------------------------------------------------------
# Connection test
# ------------------------------------------------------------------

class TestConnectionTest:

    @respx.mock
    @pytest.mark.asyncio
    async def test_success_es_only(self):
        svc = ElasticService(
            elasticsearch_url=ES_URL, api_key="k", verify_ssl=False
        )
        respx.get(f"{ES_URL}/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "cluster_name": "test-cluster",
                    "version": {"number": "8.14.0"},
                },
            )
        )

        ok, msg = await svc.test_connection()
        assert ok is True
        assert "8.14.0" in msg
        assert "test-cluster" in msg
        await svc.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_success_with_kibana(self, service):
        respx.get(f"{ES_URL}/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "cluster_name": "c",
                    "version": {"number": "8.14.0"},
                },
            )
        )
        respx.get(f"{KIBANA_URL}/api/status").mock(
            return_value=httpx.Response(
                200, json={"version": {"number": "8.14.0"}}
            )
        )

        ok, msg = await service.test_connection()
        assert ok is True
        assert "Kibana 8.14.0" in msg
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_failure_http_error(self, service):
        respx.get(f"{ES_URL}/").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        ok, msg = await service.test_connection()
        assert ok is False
        assert "401" in msg
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_failure_connection_error(self, service):
        respx.get(f"{ES_URL}/").mock(
            side_effect=httpx.ConnectError("refused")
        )

        ok, msg = await service.test_connection()
        assert ok is False
        await service.close()


# ------------------------------------------------------------------
# Elasticsearch search
# ------------------------------------------------------------------

class TestSearch:

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_returns_results(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": {
                        "total": {"value": 1},
                        "hits": [{"_id": "1", "_source": {"alert": True}}],
                    }
                },
            )
        )

        result = await service.search({"match_all": {}})
        assert result is not None
        assert result["hits"]["total"]["value"] == 1
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_custom_index(self, service):
        route = respx.post(f"{ES_URL}/my-index/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )

        await service.search({"match_all": {}}, index="my-index")
        assert route.called
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_error_returns_none(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        result = await service.search({"match_all": {}})
        assert result is None
        await service.close()


# ------------------------------------------------------------------
# IOC search helpers
# ------------------------------------------------------------------

class TestIOCSearch:

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_by_ip(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        result = await service.search_by_ip("1.2.3.4")
        assert result is not None
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_by_hash(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        result = await service.search_by_hash("abc123def456")
        assert result is not None
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_by_username(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        result = await service.search_by_username("admin")
        assert result is not None
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_by_hostname(self, service):
        respx.post(f"{ES_URL}/.alerts-security.alerts-default/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        result = await service.search_by_hostname("workstation-01")
        assert result is not None
        await service.close()


# ------------------------------------------------------------------
# get_indices
# ------------------------------------------------------------------

class TestGetIndices:

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_indices(self, service):
        respx.get(f"{ES_URL}/_cat/indices?format=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"index": ".alerts-security.alerts-default"},
                    {"index": "filebeat-8.14.0"},
                ],
            )
        )
        indices = await service.get_indices()
        assert indices == [
            ".alerts-security.alerts-default",
            "filebeat-8.14.0",
        ]
        await service.close()


# ------------------------------------------------------------------
# Kibana Detections API
# ------------------------------------------------------------------

class TestDetectionAlerts:

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_alerts(self, service):
        respx.post(
            f"{KIBANA_URL}/api/detection_engine/signals/search"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": {
                        "total": {"value": 2},
                        "hits": [
                            {"_id": "a1", "_source": {}},
                            {"_id": "a2", "_source": {}},
                        ],
                    }
                },
            )
        )

        result = await service.fetch_detection_alerts()
        assert result is not None
        assert result["hits"]["total"]["value"] == 2
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_alert_status(self, service):
        respx.post(
            f"{KIBANA_URL}/api/detection_engine/signals/status"
        ).mock(return_value=httpx.Response(200, json={}))

        ok = await service.update_alert_status(["a1", "a2"], "closed")
        assert ok is True
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_alert_status_failure(self, service):
        respx.post(
            f"{KIBANA_URL}/api/detection_engine/signals/status"
        ).mock(return_value=httpx.Response(403, text="Forbidden"))

        ok = await service.update_alert_status(["a1"], "closed")
        assert ok is False
        await service.close()


# ------------------------------------------------------------------
# Kibana Cases API
# ------------------------------------------------------------------

class TestCasesAPI:

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_case(self, service):
        respx.get(f"{KIBANA_URL}/api/cases/case-1").mock(
            return_value=httpx.Response(
                200, json={"id": "case-1", "status": "open", "version": "v1"}
            )
        )

        case = await service.get_case("case-1")
        assert case is not None
        assert case["id"] == "case-1"
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_case_status(self, service):
        respx.patch(f"{KIBANA_URL}/api/cases").mock(
            return_value=httpx.Response(200, json=[{"id": "case-1"}])
        )

        ok = await service.update_case_status("case-1", "closed", "v1")
        assert ok is True
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_case_status_failure(self, service):
        respx.patch(f"{KIBANA_URL}/api/cases").mock(
            return_value=httpx.Response(409, text="Conflict")
        )

        ok = await service.update_case_status("case-1", "closed", "v1")
        assert ok is False
        await service.close()


# ------------------------------------------------------------------
# close()
# ------------------------------------------------------------------

class TestClose:

    @pytest.mark.asyncio
    async def test_close_without_clients(self, service):
        """close() should not raise when no clients have been created."""
        await service.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_close_after_use(self, service):
        respx.get(f"{ES_URL}/").mock(
            return_value=httpx.Response(
                200,
                json={"cluster_name": "c", "version": {"number": "8.14.0"}},
            )
        )
        respx.get(f"{KIBANA_URL}/api/status").mock(
            return_value=httpx.Response(
                200, json={"version": {"number": "8.14.0"}}
            )
        )

        await service.test_connection()
        await service.close()

        assert service._es_client.is_closed
        assert service._kibana_client.is_closed
