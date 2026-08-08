"""
Unit tests for finding management logic.
Tests severity normalization, IOC extraction, deduplication, and enrichment.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch

# Skip all tests until IngestionService API is documented
pytestmark = pytest.mark.skip(reason="IngestionService methods don't exist - needs rewrite for current API")

# IngestionService doesn't exist - needs to be replaced with current ingestion logic
# from core.ingestion.ingestion_service import IngestionService


class TestSeverityNormalization:
    """Test severity level normalization across different sources."""
    
    def test_normalize_severity_splunk(self):
        """Test normalizing Splunk severity levels."""
        assert IngestionService.normalize_severity("informational", "splunk") == "low"
        assert IngestionService.normalize_severity("low", "splunk") == "low"
        assert IngestionService.normalize_severity("medium", "splunk") == "medium"
        assert IngestionService.normalize_severity("high", "splunk") == "high"
        assert IngestionService.normalize_severity("critical", "splunk") == "critical"
    
    def test_normalize_severity_azure_sentinel(self):
        """Test normalizing Azure Sentinel severity levels."""
        assert IngestionService.normalize_severity("Informational", "azure_sentinel") == "low"
        assert IngestionService.normalize_severity("Low", "azure_sentinel") == "low"
        assert IngestionService.normalize_severity("Medium", "azure_sentinel") == "medium"
        assert IngestionService.normalize_severity("High", "azure_sentinel") == "high"
    
    def test_normalize_severity_crowdstrike(self):
        """Test normalizing CrowdStrike severity levels."""
        # CrowdStrike uses numeric scores
        assert IngestionService.normalize_severity(25, "crowdstrike") == "low"
        assert IngestionService.normalize_severity(50, "crowdstrike") == "medium"
        assert IngestionService.normalize_severity(75, "crowdstrike") == "high"
        assert IngestionService.normalize_severity(95, "crowdstrike") == "critical"
    
    def test_normalize_severity_unknown_source(self):
        """Test normalizing severity from unknown source."""
        result = IngestionService.normalize_severity("high", "unknown_source")
        assert result in ["low", "medium", "high", "critical"]


class TestIOCExtraction:
    """Test extraction of Indicators of Compromise."""
    
    def test_extract_ip_addresses(self):
        """Test extracting IP addresses from raw data."""
        raw_data = {
            "src_ip": "10.0.1.5",
            "dst_ip": "185.220.101.5",
            "message": "Connection from 192.168.1.100"
        }
        
        ips = IngestionService.extract_ips(raw_data)
        
        assert "10.0.1.5" in ips
        assert "185.220.101.5" in ips
        assert "192.168.1.100" in ips
    
    def test_extract_domains(self):
        """Test extracting domains from raw data."""
        raw_data = {
            "url": "https://malicious-site.com/payload",
            "dns_query": "c2-server.example.com",
            "message": "Connected to evil.org"
        }
        
        domains = IngestionService.extract_domains(raw_data)
        
        assert "malicious-site.com" in domains
        assert "c2-server.example.com" in domains
        assert "evil.org" in domains
    
    def test_extract_file_hashes(self):
        """Test extracting file hashes from raw data."""
        raw_data = {
            "md5": "5d41402abc4b2a76b9719d911017c592",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "message": "Hash: a1b2c3d4e5f6"
        }
        
        hashes = IngestionService.extract_hashes(raw_data)
        
        assert "5d41402abc4b2a76b9719d911017c592" in hashes
        assert "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" in hashes
    
    def test_extract_users(self):
        """Test extracting usernames from raw data."""
        raw_data = {
            "user": "john.doe",
            "target_user": "admin",
            "message": "Login attempt by contractor.user"
        }
        
        users = IngestionService.extract_users(raw_data)
        
        assert "john.doe" in users
        assert "admin" in users
        assert "contractor.user" in users
    
    def test_extract_all_iocs(self):
        """Test extracting all IOC types at once."""
        raw_data = {
            "src_ip": "10.0.1.5",
            "domain": "malicious.com",
            "file_hash": "abc123",
            "user": "attacker"
        }
        
        iocs = IngestionService.extract_all_iocs(raw_data)
        
        assert "ips" in iocs
        assert "domains" in iocs
        assert "hashes" in iocs
        assert "users" in iocs


class TestDeduplication:
    """Test finding deduplication logic."""
    
    def test_calculate_dedup_hash(self):
        """Test calculating deduplication hash."""
        finding = {
            "source": "splunk",
            "external_id": "evt-12345",
            "timestamp": "2026-01-27T10:00:00Z"
        }
        
        hash1 = IngestionService.calculate_dedup_hash(finding)
        hash2 = IngestionService.calculate_dedup_hash(finding)
        
        # Same finding should produce same hash
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex digest length
    
    def test_dedup_hash_uniqueness(self):
        """Test that different findings produce different hashes."""
        finding1 = {"source": "splunk", "external_id": "evt-001"}
        finding2 = {"source": "splunk", "external_id": "evt-002"}
        
        hash1 = IngestionService.calculate_dedup_hash(finding1)
        hash2 = IngestionService.calculate_dedup_hash(finding2)
        
        assert hash1 != hash2
    
    def test_is_duplicate_true(self):
        """Test duplicate detection returns True for duplicates."""
        existing_hashes = {"abc123", "def456", "ghi789"}
        new_hash = "abc123"
        
        is_dup = IngestionService.is_duplicate(new_hash, existing_hashes)
        
        assert is_dup is True
    
    def test_is_duplicate_false(self):
        """Test duplicate detection returns False for new findings."""
        existing_hashes = {"abc123", "def456"}
        new_hash = "xyz999"
        
        is_dup = IngestionService.is_duplicate(new_hash, existing_hashes)
        
        assert is_dup is False


class TestMITREMapping:
    """Test MITRE ATT&CK technique mapping."""
    
    def test_map_technique_from_keywords(self):
        """Test mapping MITRE techniques from keywords."""
        finding = {
            "title": "PowerShell Execution",
            "description": "Encoded PowerShell command detected"
        }
        
        techniques = IngestionService.map_mitre_techniques(finding)
        
        assert "T1059.001" in techniques  # PowerShell
        assert "T1027" in techniques  # Obfuscation (encoded)
    
    def test_map_technique_from_behavior(self):
        """Test mapping techniques from behavioral indicators."""
        finding = {
            "title": "Lateral Movement",
            "raw_data": {
                "protocol": "RDP",
                "src_host": "workstation",
                "dst_host": "server"
            }
        }
        
        techniques = IngestionService.map_mitre_techniques(finding)
        
        assert "T1021.001" in techniques  # RDP
    
    def test_map_technique_c2_communication(self):
        """Test mapping C2 communication techniques."""
        finding = {
            "title": "C2 Beacon",
            "description": "Regular outbound connections to suspicious IP",
            "raw_data": {"dst_port": 443, "frequency": "60 seconds"}
        }
        
        techniques = IngestionService.map_mitre_techniques(finding)
        
        assert "T1071.001" in techniques  # Web protocols
        assert "T1573.001" in techniques  # Encrypted channel
    
    def test_map_technique_no_match(self):
        """Test handling when no techniques match."""
        finding = {
            "title": "Generic Alert",
            "description": "Something happened"
        }
        
        techniques = IngestionService.map_mitre_techniques(finding)
        
        assert isinstance(techniques, list)
        # May be empty or have generic techniques


class TestEmbeddingGeneration:
    """Test finding embedding generation for similarity search."""
    
    @patch('core.ingestion.ingestion_service.embedding_model')
    def test_generate_embedding(self, mock_model):
        """Test generating embedding vector for finding."""
        mock_model.encode.return_value = [0.1, 0.2, 0.3, 0.4, 0.5]
        
        finding = {
            "title": "Test Finding",
            "description": "Test description"
        }
        
        embedding = IngestionService.generate_embedding(finding)
        
        assert embedding is not None
        assert len(embedding) == 5
        assert all(isinstance(x, (int, float)) for x in embedding)
    
    def test_embedding_text_preparation(self):
        """Test preparing text for embedding generation."""
        finding = {
            "title": "Malware Detection",
            "description": "Ransomware found on workstation",
            "severity": "critical"
        }
        
        text = IngestionService.prepare_embedding_text(finding)
        
        assert "Malware Detection" in text
        assert "Ransomware" in text
        assert "critical" in text


class TestFindingEnrichment:
    """Test finding enrichment logic."""
    
    def test_should_enrich_high_severity(self):
        """Test that high severity findings should be enriched."""
        finding = {"severity": "high"}
        
        should_enrich = IngestionService.should_auto_enrich(finding)
        
        assert should_enrich is True
    
    def test_should_not_enrich_low_severity(self):
        """Test that low severity findings may not be auto-enriched."""
        finding = {"severity": "low"}
        
        should_enrich = IngestionService.should_auto_enrich(finding)
        
        # Implementation dependent - might be False to save API calls
        assert isinstance(should_enrich, bool)
    
    def test_enrich_with_threat_intel(self):
        """Test enriching finding with threat intelligence."""
        finding = {
            "iocs": {
                "ips": ["185.220.101.5"],
                "domains": ["malicious.com"]
            }
        }
        
        with patch('core.ingestion.ingestion_service.threat_intel') as mock_ti:
            mock_ti.check_ip.return_value = {"reputation": "malicious", "score": 95}
            
            enriched = IngestionService.enrich_with_threat_intel(finding)
            
            assert "threat_intel" in enriched
            assert enriched["threat_intel"]["ips"]["185.220.101.5"]["reputation"] == "malicious"


class TestBatchProcessing:
    """Test batch processing of findings."""
    
    def test_batch_findings(self):
        """Test splitting findings into batches."""
        findings = [{"id": f"finding-{i}"} for i in range(25)]
        batch_size = 10
        
        batches = IngestionService.create_batches(findings, batch_size)
        
        assert len(batches) == 3
        assert len(batches[0]) == 10
        assert len(batches[1]) == 10
        assert len(batches[2]) == 5
    
    def test_process_batch_success(self):
        """Test successful batch processing."""
        findings = [
            {"id": "finding-1", "severity": "high"},
            {"id": "finding-2", "severity": "medium"}
        ]
        
        results = IngestionService.process_batch(findings)
        
        assert len(results) == 2
        assert all(r["status"] == "success" for r in results)
    
    def test_process_batch_with_failures(self):
        """Test batch processing with some failures."""
        findings = [
            {"id": "finding-1", "severity": "high"},
            {"id": "finding-2"},  # Missing severity
            {"id": "finding-3", "severity": "low"}
        ]
        
        results = IngestionService.process_batch(findings, fail_on_error=False)
        
        assert len(results) == 3
        # Should have mix of success and failure
        statuses = [r["status"] for r in results]
        assert "success" in statuses


@pytest.mark.unit
class TestIngestionWorkflow:
    """Test complete ingestion workflow."""
    
    def test_full_ingestion_pipeline(self):
        """Test complete finding ingestion pipeline."""
        raw_finding = {
            "source": "splunk",
            "external_id": "evt-12345",
            "severity": "high",
            "src_ip": "10.0.1.5",
            "dst_ip": "185.220.101.5",
            "message": "Suspicious PowerShell execution"
        }
        
        # Normalize severity
        normalized_severity = IngestionService.normalize_severity(
            raw_finding["severity"], raw_finding["source"]
        )
        assert normalized_severity == "high"
        
        # Extract IOCs
        iocs = IngestionService.extract_all_iocs(raw_finding)
        assert "10.0.1.5" in iocs["ips"]
        
        # Map MITRE techniques
        techniques = IngestionService.map_mitre_techniques(raw_finding)
        assert len(techniques) > 0
        
        # Calculate dedup hash
        dedup_hash = IngestionService.calculate_dedup_hash(raw_finding)
        assert len(dedup_hash) == 64
        
        # Check if should enrich
        should_enrich = IngestionService.should_auto_enrich(
            {"severity": normalized_severity}
        )
        assert should_enrich is True

