"""Unit tests for bi-directional case-status sync (sub-issue #68)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ------------------------------------------------------------------
# Base class contract
# ------------------------------------------------------------------

class TestBaseClassContract:

    def test_update_upstream_raises_not_implemented(self):
        """Default implementation should raise NotImplementedError."""
        from services.siem_ingestion_service import SIEMIngestionService

        class StubSIEM(SIEMIngestionService):
            async def fetch_alerts(self, **kw):
                return []

            def transform_alert_to_finding(self, alert):
                return None

        svc = StubSIEM.__new__(StubSIEM)
        svc.siem_name = "Stub"

        with pytest.raises(NotImplementedError, match="Stub"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                svc.update_upstream_alert_status("a1", "closed")
            )


# ------------------------------------------------------------------
# Elastic implementation
# ------------------------------------------------------------------

class TestElasticUpstreamSync:

    @pytest.mark.asyncio
    async def test_sync_closed(self):
        with patch("services.elastic_ingestion.get_integration_config") as cfg:
            cfg.return_value = {
                "elasticsearch_url": "https://es.test:9200",
                "kibana_url": "https://kibana.test:5601",
                "api_key": "k",
            }
            from services.elastic_ingestion import ElasticIngestion

            svc = ElasticIngestion()
            svc.ingestion_service = MagicMock()
            mock_es = MagicMock()
            mock_es.update_alert_status = AsyncMock(return_value=True)
            svc._elastic_service = mock_es

            result = await svc.update_upstream_alert_status("alert-1", "closed")
            assert result is True
            mock_es.update_alert_status.assert_called_once_with(
                ["alert-1"], "closed"
            )

    @pytest.mark.asyncio
    async def test_sync_maps_resolved_to_closed(self):
        with patch("services.elastic_ingestion.get_integration_config") as cfg:
            cfg.return_value = {
                "elasticsearch_url": "https://es.test:9200",
                "kibana_url": "https://kibana.test:5601",
                "api_key": "k",
            }
            from services.elastic_ingestion import ElasticIngestion

            svc = ElasticIngestion()
            svc.ingestion_service = MagicMock()
            mock_es = MagicMock()
            mock_es.update_alert_status = AsyncMock(return_value=True)
            svc._elastic_service = mock_es

            await svc.update_upstream_alert_status("alert-1", "resolved")
            mock_es.update_alert_status.assert_called_once_with(
                ["alert-1"], "closed"
            )

    @pytest.mark.asyncio
    async def test_sync_maps_new_to_open(self):
        with patch("services.elastic_ingestion.get_integration_config") as cfg:
            cfg.return_value = {
                "elasticsearch_url": "https://es.test:9200",
                "kibana_url": "https://kibana.test:5601",
                "api_key": "k",
            }
            from services.elastic_ingestion import ElasticIngestion

            svc = ElasticIngestion()
            svc.ingestion_service = MagicMock()
            mock_es = MagicMock()
            mock_es.update_alert_status = AsyncMock(return_value=True)
            svc._elastic_service = mock_es

            await svc.update_upstream_alert_status("alert-1", "new")
            mock_es.update_alert_status.assert_called_once_with(
                ["alert-1"], "open"
            )

    @pytest.mark.asyncio
    async def test_returns_false_when_service_unavailable(self):
        with patch("services.elastic_ingestion.get_integration_config") as cfg:
            cfg.return_value = {}
            from services.elastic_ingestion import ElasticIngestion

            svc = ElasticIngestion()
            svc.ingestion_service = MagicMock()
            svc._elastic_service = None

            with patch.object(svc, "_get_elastic_service", return_value=None):
                result = await svc.update_upstream_alert_status("a1", "closed")
                assert result is False
