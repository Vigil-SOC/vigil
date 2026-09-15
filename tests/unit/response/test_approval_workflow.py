"""Unit tests for the autonomous response correlation logic."""

from core.response.autonomous_response_service import AutonomousResponseService


class TestAutonomousResponse:
    """Test autonomous response service."""

    def test_correlate_alerts(self):
        """Test correlating alerts from multiple sources."""
        service = AutonomousResponseService()

        # Create mock alert data matching the actual method signature
        tempo_flow_alert = {
            "finding_id": "f-12345",
            "severity": "high",
            "mitre_predictions": {"T1486": "ransomware", "T1071": "c2_communication"},
            "entity_context": {"src_ips": ["10.0.1.5"]},
        }

        correlation = service.correlate_alerts(tempo_flow_alert=tempo_flow_alert)

        # Check that correlation returns expected structure
        assert "confidence" in correlation
        assert "indicators" in correlation
        assert "evidence" in correlation
        assert correlation["confidence"] > 0
