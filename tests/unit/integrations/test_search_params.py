"""Index and time-range validation shared by the Elastic and OpenSearch tools."""

import json

import httpx
import pytest
import respx

from core.integrations._base.search_params import validate_index, validate_time_range
from core.integrations.elastic import tool as elastic_tool
from core.integrations.elastic.client import ElasticService
from core.integrations.opensearch.client import LOG_INDICES, OpenSearchService

pytestmark = pytest.mark.unit

ES_URL = "https://es.test:9200"
OS_URL = "https://os.test:9200"

OK_INDICES = [
    "my-index",
    "wazuh-alerts-4.x-*",
    ".alerts-security.alerts-default",
    ".opensearch-sap-*-findings-*",
    "logs-*,-logs-debug*",
    "remote:logs-*",
    LOG_INDICES,
]
BAD_INDICES = [
    "victim/_delete_by_query#",
    "victim/_delete_by_query?x=",
    "victim#",
    "a/b",
    "../_cluster",
    "..",
    ".",
    "_all",
    "ok,_cat",
    "ok,-_cat",
    "a b",
    "a%2f_delete_by_query",
    "",
]


@pytest.mark.parametrize("index", OK_INDICES)
def test_valid_index_names_pass(index):
    assert validate_index(index) == index


@pytest.mark.parametrize("index", BAD_INDICES)
def test_path_injection_index_names_are_rejected(index):
    with pytest.raises(ValueError):
        validate_index(index)


@pytest.mark.parametrize("value", ["24h", "7d", "90m", "1y", "2w"])
def test_valid_time_ranges_pass(value):
    assert validate_time_range(value) == value


@pytest.mark.parametrize(
    "value", ["", "h", "24", "24h/d", "1d+1d", '1h"}', "100y ", "-1d"]
)
def test_bad_time_ranges_are_rejected(value):
    with pytest.raises(ValueError):
        validate_time_range(value)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,url",
    [
        (ElasticService(elasticsearch_url=ES_URL, api_key="k"), ES_URL),
        (OpenSearchService(opensearch_url=OS_URL, username="u", password="p"), OS_URL),
    ],
)
async def test_search_never_sends_a_crafted_index(service, url):
    route = respx.route(host=httpx.URL(url).host).mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    with pytest.raises(ValueError):
        await service.search({"match_all": {}}, index="victim/_delete_by_query#")
    assert not route.called
    await service.close()


@respx.mock
@pytest.mark.asyncio
async def test_search_tool_rejects_crafted_index_and_time_range():
    svc = ElasticService(elasticsearch_url=ES_URL, api_key="k")
    route = respx.route(host="es.test").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": []}})
    )
    query = json.dumps({"match_all": {}})
    with pytest.raises(ValueError):
        await elastic_tool._search_logs(
            svc, {"query": query, "index": "victim/_delete_by_query#"}
        )
    with pytest.raises(ValueError):
        await elastic_tool._search_logs(svc, {"query": query, "time_range": "100y/d"})
    assert not route.called
    await svc.close()


@pytest.mark.asyncio
async def test_hours_must_be_an_integer():
    svc = ElasticService(elasticsearch_url=ES_URL, api_key="k")
    with pytest.raises(ValueError):
        await svc.search_by_ip("1.2.3.4", hours="1d/d")
    await svc.close()
