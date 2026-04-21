"""Unit tests for services/elastic_enrichment_service.py."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.elastic_enrichment_service import ElasticEnrichmentService, _is_private_ip


@pytest.fixture
def enrichment_svc():
    mock_elastic = MagicMock()
    svc = ElasticEnrichmentService(elastic_service=mock_elastic)
    svc.data_service = MagicMock()
    return svc


class TestExtractIndicators:

    def test_extracts_ips_from_entity_context(self, enrichment_svc):
        case = {}
        findings = [
            {
                "entity_context": {
                    "src_ips": ["203.0.113.10"],
                    "dest_ips": ["198.51.100.5"],
                    "usernames": ["jdoe"],
                    "hostnames": ["web-server-01"],
                }
            }
        ]
        indicators = enrichment_svc.extract_indicators(case, findings)
        assert "203.0.113.10" in indicators["ips"]
        assert "198.51.100.5" in indicators["ips"]
        assert "jdoe" in indicators["usernames"]
        assert "web-server-01" in indicators["hostnames"]

    def test_filters_private_ips(self, enrichment_svc):
        findings = [
            {
                "entity_context": {
                    "src_ips": ["10.0.1.1", "203.0.113.10"],
                    "dest_ips": ["192.168.1.1"],
                }
            }
        ]
        indicators = enrichment_svc.extract_indicators({}, findings)
        assert "10.0.1.1" not in indicators["ips"]
        assert "192.168.1.1" not in indicators["ips"]
        assert "203.0.113.10" in indicators["ips"]

    def test_extracts_hashes_from_text(self, enrichment_svc):
        case = {"description": "File hash: d41d8cd98f00b204e9800998ecf8427e"}
        indicators = enrichment_svc.extract_indicators(case, [])
        assert "d41d8cd98f00b204e9800998ecf8427e" in indicators["hashes"]


class TestQueryElasticForIndicators:

    @pytest.mark.asyncio
    async def test_queries_all_indicator_types(self, enrichment_svc):
        mock_elastic = enrichment_svc.elastic_service
        mock_elastic.search_by_ip = AsyncMock(
            return_value={"hits": {"hits": [{"_id": "1", "_source": {}}]}}
        )
        mock_elastic.search_by_hash = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        mock_elastic.search_by_username = AsyncMock(
            return_value={"hits": {"hits": [{"_id": "2", "_source": {}}]}}
        )
        mock_elastic.search_by_hostname = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        indicators = {
            "ips": ["203.0.113.10"],
            "hashes": ["abc123"],
            "usernames": ["admin"],
            "hostnames": ["server-01"],
        }
        result = await enrichment_svc.query_elastic_for_indicators(indicators)
        assert result["summary"]["total_events"] == 2
        assert result["summary"]["ips_queried"] == 1
        mock_elastic.search_by_ip.assert_called_once()

    @pytest.mark.asyncio
    async def test_limits_indicators_to_ten(self, enrichment_svc):
        mock_elastic = enrichment_svc.elastic_service
        mock_elastic.search_by_ip = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        indicators = {"ips": [f"1.2.3.{i}" for i in range(20)]}
        await enrichment_svc.query_elastic_for_indicators(indicators)
        assert mock_elastic.search_by_ip.call_count == 10


class TestIsPrivateIP:

    def test_private_10(self):
        assert _is_private_ip("10.0.0.1") is True

    def test_private_172(self):
        assert _is_private_ip("172.16.0.1") is True

    def test_private_192(self):
        assert _is_private_ip("192.168.1.1") is True

    def test_loopback(self):
        assert _is_private_ip("127.0.0.1") is True

    def test_public(self):
        assert _is_private_ip("203.0.113.10") is False

    def test_invalid(self):
        assert _is_private_ip("not-an-ip") is True
