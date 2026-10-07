"""MCP tool server reads Elastic config through resolve(), not ELASTIC_* env."""

from unittest.mock import patch

import pytest

from core.integrations.elastic import tool as elastic_tool

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cached_service():
    elastic_tool._elastic_service = None
    yield
    elastic_tool._elastic_service = None


def _resolved(**overrides):
    config = {
        "elasticsearch_url": "https://es.test:9200",
        "kibana_url": "https://kibana.test:5601",
        "api_key": "secret-from-store",
        "username": None,
        "password": None,
        "index_pattern": None,
        "verify_ssl": None,
    }
    config.update(overrides)
    return config


def test_resolved_fields_reach_the_client():
    with patch.object(elastic_tool, "resolve", return_value=_resolved()):
        svc = elastic_tool.get_elastic_service()
    assert svc is not None
    assert svc.elasticsearch_url == "https://es.test:9200"
    assert svc.api_key == "secret-from-store"
    assert svc.verify_ssl is True
    assert svc.index_pattern == ".alerts-security.alerts-default"


def test_verify_ssl_false_is_preserved():
    with patch.object(
        elastic_tool, "resolve", return_value=_resolved(verify_ssl=False)
    ):
        svc = elastic_tool.get_elastic_service()
    assert svc is not None
    assert svc.verify_ssl is False


def test_missing_url_is_not_configured():
    with patch.object(
        elastic_tool, "resolve", return_value=_resolved(elasticsearch_url=None)
    ):
        assert elastic_tool.get_elastic_service() is None
