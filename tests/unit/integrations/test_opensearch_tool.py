"""MCP tool server reads OpenSearch config through resolve(), not env."""

import asyncio
from unittest.mock import patch

import pytest

from core.integrations.opensearch import tool as opensearch_tool

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cached_service():
    opensearch_tool._opensearch_service = None
    yield
    opensearch_tool._opensearch_service = None


def _resolved(**overrides):
    config = {
        "opensearch_url": "https://os.test:9200",
        "dashboards_url": "https://dashboards.test:5601",
        "username": "admin",
        "password": "secret-from-store",
        "index_pattern": None,
        "verify_ssl": None,
        "ca_cert_path": None,
    }
    config.update(overrides)
    return config


def test_resolved_fields_reach_the_client():
    with patch.object(opensearch_tool, "resolve", return_value=_resolved()):
        svc = opensearch_tool.get_opensearch_service()
    assert svc is not None
    assert svc.opensearch_url == "https://os.test:9200"
    assert svc.password == "secret-from-store"
    assert svc.verify_ssl is True
    assert svc.index_pattern == ".opensearch-sap-*-findings-*"


def test_verify_ssl_false_is_preserved():
    with patch.object(
        opensearch_tool, "resolve", return_value=_resolved(verify_ssl=False)
    ):
        svc = opensearch_tool.get_opensearch_service()
    assert svc is not None
    assert svc.verify_ssl is False


def test_missing_url_is_not_configured():
    with patch.object(
        opensearch_tool, "resolve", return_value=_resolved(opensearch_url=None)
    ):
        assert opensearch_tool.get_opensearch_service() is None


def test_lists_the_four_tools():
    tools = asyncio.run(opensearch_tool.handle_list_tools())
    assert [t.name for t in tools] == [
        "opensearch_search_logs",
        "opensearch_search_by_ioc",
        "opensearch_get_indices",
        "opensearch_get_findings",
    ]


def test_findings_filter_by_detector_name_uses_monitor_name():
    class FakeService:
        async def search(self, query, size, sort):
            self.query = query
            return {"hits": {"total": {"value": 0}, "hits": []}}

    fake = FakeService()
    opensearch_tool._opensearch_service = fake
    asyncio.run(
        opensearch_tool.handle_call_tool(
            "opensearch_get_findings", {"detector_name": "win-detector"}
        )
    )
    # Findings carry monitor_name (the detector's name), not a detector id.
    assert fake.query == {"term": {"monitor_name": "win-detector"}}
