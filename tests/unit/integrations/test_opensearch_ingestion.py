"""Unit tests for core/integrations/opensearch/ingestion.py."""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.integrations.opensearch.ingestion import (
    OpenSearchIngestion,
    finding_time,
    log_type_of,
    mitre_from_tags,
    rule_ids_of,
    severity_of,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


@pytest.fixture
def sample_findings():
    with open(FIXTURES_DIR / "opensearch_findings.json") as f:
        return json.load(f)


@pytest.fixture
def ingestion():
    with patch("core.integrations.opensearch.ingestion.resolve") as mock_resolve:
        mock_resolve.return_value = {
            "opensearch_url": "https://os.test:9200",
            "dashboards_url": None,
            "username": "admin",
            "password": "secret-from-store",
            "index_pattern": None,
            "verify_ssl": None,
            "ca_cert_path": None,
        }
        svc = OpenSearchIngestion()
        # Prevent actual IngestionService init side effects
        svc.ingestion_service = MagicMock()
        yield svc


class TestTransformFinding:

    def test_transforms_finding_fields(self, ingestion, sample_findings):
        finding = ingestion.transform_alert_to_finding(sample_findings[0])
        assert finding is not None
        assert finding["finding_id"] == "opensearch-f1nd1ng-0001"
        assert finding["external_id"] == "f1nd1ng-0001"
        assert finding["data_source"] == "opensearch"
        assert finding["title"] == "Suspicious PowerShell Execution"
        assert finding["severity"] == "high"
        assert finding["timestamp"] == "2026-10-04T15:04:05.123000Z"

    def test_metadata_carries_finding_context(self, ingestion, sample_findings):
        finding = ingestion.transform_alert_to_finding(sample_findings[0])
        metadata = finding["metadata"]
        assert metadata["opensearch_finding_id"] == "f1nd1ng-0001"
        assert metadata["monitor_id"] == "monitor-abc"
        assert metadata["detector_name"] == "win-detector"
        assert metadata["triggered_rule_ids"] == ["rule-uuid-001"]
        assert metadata["related_doc_ids"] == ["doc-1", "doc-2"]
        assert metadata["source_index"] == "windows-logs"
        assert metadata["log_type"] == "windows"

    def test_extracts_mitre_techniques_from_sigma_tags(
        self, ingestion, sample_findings
    ):
        finding = ingestion.transform_alert_to_finding(sample_findings[0])
        assert finding["mitre_predictions"] == {"T1059.001": 0.9}

    def test_bare_rule_reference_falls_back(self, ingestion, sample_findings):
        """A finding whose queries carry only rule ids still transforms."""
        finding = ingestion.transform_alert_to_finding(sample_findings[1])
        assert finding is not None
        assert finding["title"] == "OpenSearch Security Analytics Finding"
        assert finding["severity"] == "medium"
        assert finding["metadata"]["triggered_rule_ids"] == ["rule-uuid-002"]
        assert finding["metadata"]["log_type"] == "network"

    def test_handles_missing_fields_gracefully(self, ingestion):
        finding = ingestion.transform_alert_to_finding(
            {"_id": "sparse-1", "_source": {}}
        )
        assert finding is not None
        assert finding["finding_id"] == "opensearch-sparse-1"
        assert finding["title"] == "OpenSearch Security Analytics Finding"
        assert finding["severity"] == "medium"
        assert finding["entity_context"]["src_ips"] == []

    def test_long_finding_id_fits_finding_id_column(self, ingestion):
        finding_ref = hashlib.sha256(b"x").hexdigest()
        alert = {
            "_id": finding_ref,
            "_index": ".opensearch-sap-windows-findings-2026.10.04",
            "_source": {"id": finding_ref, "timestamp": 1791126245000},
        }
        finding = ingestion.transform_alert_to_finding(alert)
        assert finding is not None
        # findings.finding_id is String(50)
        assert len(finding["finding_id"]) <= 50
        assert finding["finding_id"].startswith("opensearch-")
        # The full id survives for dedup.
        assert finding["external_id"] == finding_ref
        assert finding["metadata"]["opensearch_finding_id"] == finding_ref

    def test_handles_transform_error(self, ingestion):
        finding = ingestion.transform_alert_to_finding(None)
        assert finding is None


class TestFindingHelpers:

    def test_rule_ids_of_accepts_bare_strings(self):
        assert rule_ids_of({"queries": ["r1", {"id": "r2"}]}) == ["r1", "r2"]

    def test_severity_of_reads_sigma_level_from_rule_tags(self):
        source = {"queries": [{"tags": ["critical", "windows", "attack.t1003"]}]}
        assert severity_of(source) == "critical"
        assert severity_of({"queries": [{"tags": ["windows"]}]}) is None
        assert severity_of({}) is None

    def test_finding_time_reads_epoch_milliseconds(self):
        assert finding_time({"timestamp": 1791126245123}) == datetime(
            2026, 10, 4, 15, 4, 5, 123000
        )
        assert finding_time({"timestamp": "2026-10-04T15:04:05Z"}) is None
        assert finding_time({}) is None

    def test_mitre_from_tags_ignores_non_technique_tags(self):
        source = {"queries": [{"tags": ["attack.execution", "attack.t1110.001"]}]}
        assert mitre_from_tags(source) == {"T1110.001": 0.9}

    def test_log_type_of(self):
        assert log_type_of(".opensearch-sap-ad_ldap-findings-2026.10.04") == "ad_ldap"
        assert log_type_of("windows-logs") == ""


class TestGetOpenSearchService:

    def test_resolved_password_reaches_the_client(self, ingestion):
        svc = ingestion._get_opensearch_service()
        assert svc is not None
        assert svc.opensearch_url == "https://os.test:9200"
        assert svc.password == "secret-from-store"
        assert svc.verify_ssl is True
        assert svc.index_pattern == ".opensearch-sap-*-findings-*"

    def test_returns_none_without_opensearch_url(self):
        with patch("core.integrations.opensearch.ingestion.resolve") as mock_resolve:
            mock_resolve.return_value = {
                "opensearch_url": None,
                "dashboards_url": None,
                "username": None,
                "password": None,
                "index_pattern": None,
                "verify_ssl": None,
                "ca_cert_path": None,
            }
            svc = OpenSearchIngestion()
            assert svc._get_opensearch_service() is None

    def test_verify_ssl_false_is_preserved(self):
        with patch("core.integrations.opensearch.ingestion.resolve") as mock_resolve:
            mock_resolve.return_value = {
                "opensearch_url": "https://os.test:9200",
                "dashboards_url": None,
                "username": None,
                "password": None,
                "index_pattern": None,
                "verify_ssl": False,
                "ca_cert_path": None,
            }
            svc = OpenSearchIngestion()
            assert svc._get_opensearch_service().verify_ssl is False


class TestFetchAlerts:

    @pytest.mark.asyncio
    async def test_fetch_searches_timestamp_window_oldest_first(
        self, ingestion, sample_findings
    ):
        mock_svc = MagicMock()
        mock_svc.index_pattern = ".opensearch-sap-*-findings-*"
        mock_svc.search = AsyncMock(return_value={"hits": {"hits": sample_findings}})
        ingestion._opensearch_service = mock_svc

        start = datetime(2026, 10, 4, 14, 0, 0)
        end = start + timedelta(hours=1)
        hits = await ingestion.fetch_alerts(
            start_time=start, end_time=end, limit=10, oldest_first=True
        )
        assert hits == sample_findings

        query = mock_svc.search.call_args.kwargs["query"]
        range_filter = query["bool"]["filter"][0]["range"]["timestamp"]
        # The findings index maps ``timestamp`` as a long of epoch ms, which
        # rejects an ISO string bound.
        assert range_filter["gte"] == 1791122400000
        assert range_filter["lte"] == 1791126000000
        sort = mock_svc.search.call_args.kwargs["sort"]
        assert sort[0] == {"timestamp": {"order": "asc"}}

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_no_service(self, ingestion):
        ingestion.config = {"opensearch_url": None}
        assert await ingestion.fetch_alerts() == []

    @pytest.mark.asyncio
    async def test_fetch_raises_when_the_search_fails(self, ingestion):
        mock_svc = MagicMock()
        mock_svc.index_pattern = ".opensearch-sap-*-findings-*"
        mock_svc.search = AsyncMock(return_value=None)
        ingestion._opensearch_service = mock_svc
        with pytest.raises(RuntimeError):
            await ingestion.fetch_alerts()


class TestUpdateUpstreamAlertStatus:

    @pytest.mark.asyncio
    async def test_findings_have_no_upstream_status(self, ingestion):
        # Security Analytics findings are immutable; nothing to sync back.
        assert await ingestion.update_upstream_alert_status("f1", "closed") is False
