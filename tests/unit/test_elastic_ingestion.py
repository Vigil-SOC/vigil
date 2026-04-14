"""Unit tests for services/elastic_ingestion.py."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from services.elastic_ingestion import ElasticIngestion


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def sample_alerts():
    with open(FIXTURES_DIR / "elastic_alerts.json") as f:
        return json.load(f)


@pytest.fixture
def ingestion():
    with patch("services.elastic_ingestion.get_integration_config") as mock_cfg:
        mock_cfg.return_value = {
            "elasticsearch_url": "https://es.test:9200",
            "kibana_url": "https://kibana.test:5601",
            "api_key": "test-key",
        }
        svc = ElasticIngestion()
        # Prevent actual IngestionService init
        svc.ingestion_service = MagicMock()
        yield svc


class TestTransformAlert:

    def test_transforms_kibana_alert_fields(self, ingestion, sample_alerts):
        finding = ingestion.transform_alert_to_finding(sample_alerts[0])
        assert finding is not None
        assert finding["finding_id"] == "elastic-abc123def456"
        assert finding["data_source"] == "elastic"
        assert finding["severity"] == "high"
        assert finding["title"] == "Suspicious PowerShell Execution"
        assert "WORKSTATION-01" in finding["entity_context"]["hostnames"]
        assert "jsmith" in finding["entity_context"]["usernames"]
        assert "10.0.1.50" in finding["entity_context"]["src_ips"]
        assert "198.51.100.42" in finding["entity_context"]["dest_ips"]

    def test_extracts_mitre_techniques(self, ingestion, sample_alerts):
        finding = ingestion.transform_alert_to_finding(sample_alerts[0])
        assert "T1059.001" in finding["mitre_predictions"]

    def test_transforms_brute_force_alert(self, ingestion, sample_alerts):
        finding = ingestion.transform_alert_to_finding(sample_alerts[1])
        assert finding["severity"] == "medium"
        assert finding["title"] == "Brute Force Attempt Detected"
        assert "T1110.001" in finding["mitre_predictions"]

    def test_transforms_legacy_signal_format(self, ingestion, sample_alerts):
        """Alerts using the older signal.rule structure should still work."""
        finding = ingestion.transform_alert_to_finding(sample_alerts[2])
        assert finding is not None
        assert finding["title"] == "DNS Query to Newly Registered Domain"
        assert finding["severity"] == "low"

    def test_metadata_contains_alert_id(self, ingestion, sample_alerts):
        finding = ingestion.transform_alert_to_finding(sample_alerts[0])
        assert finding["metadata"]["elastic_alert_id"] == "abc123def456"
        assert finding["metadata"]["rule_id"] == "rule-uuid-001"

    def test_handles_missing_fields_gracefully(self, ingestion):
        sparse_alert = {"_id": "sparse-1", "_source": {}}
        finding = ingestion.transform_alert_to_finding(sparse_alert)
        assert finding is not None
        assert finding["finding_id"] == "elastic-sparse-1"
        assert finding["title"] == "Elastic Security Alert"
        assert finding["severity"] == "medium"

    def test_handles_transform_error(self, ingestion):
        # Pass completely invalid data
        finding = ingestion.transform_alert_to_finding(None)
        assert finding is None


class TestFetchAlerts:

    @pytest.mark.asyncio
    async def test_fetch_delegates_to_service(self, ingestion, sample_alerts):
        mock_svc = MagicMock()
        mock_svc.fetch_detection_alerts = AsyncMock(
            return_value={"hits": {"hits": sample_alerts}}
        )
        ingestion._elastic_service = mock_svc

        alerts = await ingestion.fetch_alerts(limit=50)
        assert len(alerts) == 3
        mock_svc.fetch_detection_alerts.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_no_service(self, ingestion):
        ingestion._elastic_service = None
        with patch.object(ingestion, "_get_elastic_service", return_value=None):
            alerts = await ingestion.fetch_alerts()
            assert alerts == []

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_error(self, ingestion):
        mock_svc = MagicMock()
        mock_svc.fetch_detection_alerts = AsyncMock(side_effect=Exception("fail"))
        ingestion._elastic_service = mock_svc

        alerts = await ingestion.fetch_alerts()
        assert alerts == []


class TestUpdateUpstreamAlertStatus:

    @pytest.mark.asyncio
    async def test_maps_vigil_status_to_elastic(self, ingestion):
        mock_svc = MagicMock()
        mock_svc.update_alert_status = AsyncMock(return_value=True)
        ingestion._elastic_service = mock_svc

        result = await ingestion.update_upstream_alert_status("a1", "closed")
        assert result is True
        mock_svc.update_alert_status.assert_called_once_with(["a1"], "closed")

    @pytest.mark.asyncio
    async def test_maps_in_progress_to_acknowledged(self, ingestion):
        mock_svc = MagicMock()
        mock_svc.update_alert_status = AsyncMock(return_value=True)
        ingestion._elastic_service = mock_svc

        await ingestion.update_upstream_alert_status("a1", "in_progress")
        mock_svc.update_alert_status.assert_called_once_with(
            ["a1"], "acknowledged"
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_no_service(self, ingestion):
        ingestion._elastic_service = None
        with patch.object(ingestion, "_get_elastic_service", return_value=None):
            result = await ingestion.update_upstream_alert_status("a1", "closed")
            assert result is False
