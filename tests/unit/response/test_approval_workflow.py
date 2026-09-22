"""
Unit tests for approval workflow logic.
Tests confidence thresholds, auto-approval, action validation, and audit trail.

Note: Tests marked with @pytest.mark.external_service require external services
(CrowdStrike, Firewall, AD, etc.) and are expected to fail until integration is complete.
"""

import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from core.config import get_settings
from core.response.approval_service import (
    ActionStatus,
    ActionType,
    ApprovalService,
    Reversibility,
)
from core.response.autonomous_response_service import AutonomousResponseService
from core.response.config import ResponseConfig


@pytest.fixture
def no_db():
    """Stand-in session and config store so ApprovalService never reaches
    PostgreSQL: not for the row _put_action inserts, and not for the
    force_manual_approval flag __init__ reads (and would otherwise write).
    The status the test reads is decided before the session is touched.
    """
    manager = MagicMock()

    @contextmanager
    def _scope():
        yield MagicMock()

    manager.session_scope = _scope
    config_store = Mock()
    config_store.get_system_config.return_value = {"enabled": False}
    with (
        patch("core.response.approval_service.get_db_manager", return_value=manager),
        patch(
            "core.response.approval_service.get_config_service",
            return_value=config_store,
        ),
    ):
        yield


def _create(svc: ApprovalService, confidence: float, **kwargs):
    return svc.create_action(
        action_type=ActionType.BLOCK_IP,
        title="block",
        description="test",
        target="1.2.3.4",
        confidence=confidence,
        reason="test",
        evidence=[],
        created_by="pytest",
        **kwargs,
    )


@pytest.mark.usefixtures("no_db")
class TestConfidenceThresholds:
    """The auto-approve line is ResponseConfig.confidence_threshold, nothing else (#916)."""

    def test_default_threshold_auto_approves_at_ninety(self):
        action = _create(ApprovalService(config=ResponseConfig()), confidence=0.90)
        assert action.status == ActionStatus.APPROVED.value
        assert action.requires_approval is False

    def test_default_threshold_holds_below_ninety(self):
        # The deleted test-only should_auto_approve had a hardcoded >= 0.85
        # branch that passed 0.87; the gate itself never did, and still does not.
        action = _create(ApprovalService(config=ResponseConfig()), confidence=0.87)
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True

    def test_raised_threshold_makes_ninety_wait_for_approval(self):
        svc = ApprovalService(config=ResponseConfig(confidence_threshold=0.95))
        action = _create(svc, confidence=0.90, reversibility=Reversibility.REVERSIBLE)
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True

    def test_no_arg_service_reads_threshold_from_env(self, monkeypatch):
        monkeypatch.setenv("DAEMON_CONFIDENCE_THRESHOLD", "0.95")
        get_settings.cache_clear()
        try:
            svc = ApprovalService()
            assert svc.config.confidence_threshold == 0.95
            action = _create(svc, confidence=0.92)
            assert action.status == ActionStatus.PENDING.value
        finally:
            get_settings.cache_clear()

    def test_force_manual_wins_over_confidence(self):
        svc = ApprovalService(config=ResponseConfig())
        svc.force_manual_approval = True
        action = svc.create_action(
            action_type=ActionType.ISOLATE_HOST,
            title="isolate",
            description="test",
            target="host-1",
            confidence=0.99,
            reason="test",
            evidence=[],
        )
        assert action.status == ActionStatus.PENDING.value


class TestConfiguredBands:
    """Every other comparison reads the same ResponseConfig (#916)."""

    def test_recommendation_ladder_follows_config(self):
        svc = AutonomousResponseService(
            approvals=Mock(spec=ApprovalService),
            config=ResponseConfig(
                confidence_threshold=0.95,
                review_threshold=0.90,
                monitor_threshold=0.80,
            ),
        )
        assert svc._get_recommendation(0.95, []).startswith("AUTO-ISOLATE")
        assert svc._get_recommendation(0.92, []).startswith("ISOLATE WITH APPROVAL")
        assert svc._get_recommendation(0.85, []).startswith("MANUAL REVIEW")
        assert svc._get_recommendation(0.79, []).startswith("MONITOR")

    def test_no_action_reason_names_the_field_and_value(self):
        svc = AutonomousResponseService(
            approvals=Mock(spec=ApprovalService),
            config=ResponseConfig(review_threshold=0.80),
        )
        finding = {
            "finding_id": "f-1",
            "severity": "low",
            "mitre_predictions": {},
            "entity_context": {"src_ips": ["10.0.0.1"]},
        }
        with patch(
            "core.storage.database_data_service.DatabaseDataService"
        ) as data_service:
            data_service.return_value.get_finding.return_value = finding
            result = svc.investigate_and_respond("f-1")
            held = svc.investigate_and_respond("f-1", auto_execute=False)
        assert result["action"]["status"] == "no_action"
        assert result["action"]["reason"] == (
            "response.review_threshold=0.80 not met (0.00)"
        )
        assert held["action"]["reason"] == "auto_execute disabled"


class TestActionValidation:
    """Test action type validation."""
    
    def test_valid_action_types(self):
        """Test valid action types."""
        service = ApprovalService()
        
        valid_types = [
            "isolate_host",
            "block_ip",
            "block_domain",
            "quarantine_file",
            "disable_user",
            "execute_spl_query",
            "custom"
        ]
        
        for action_type in valid_types:
            assert service.is_valid_action_type(action_type) is True
    
    def test_invalid_action_type(self):
        """Test invalid action type."""
        service = ApprovalService()
        
        assert service.is_valid_action_type("invalid_action") is False
    
    def test_validate_action_payload(self):
        """Test validating action payload."""
        service = ApprovalService()
        
        # Valid payload
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.90,
            "reasoning": "C2 communication detected"
        }
        
        is_valid, errors = service.validate_action(action)
        
        assert is_valid is True
        assert len(errors) == 0
    
    def test_validate_missing_required_fields(self):
        """Test validation with missing required fields."""
        service = ApprovalService()
        
        # Missing target
        action = {
            "type": "isolate_host",
            "confidence": 0.90
        }
        
        is_valid, errors = service.validate_action(action)
        
        assert is_valid is False
        assert "target" in str(errors).lower()


class TestActionExecution:
    """Test action execution logic."""
    
    @pytest.mark.skip(reason="execute_action method requires service integration - will be implemented")
    def test_execute_isolate_host(self):
        """Test executing host isolation."""
        # This test will be enabled once execute_action is fully implemented with service integrations
        pass
    
    @pytest.mark.skip(reason="execute_action method requires service integration - will be implemented")
    def test_execute_block_ip(self):
        """Test executing IP block."""
        # This test will be enabled once execute_action is fully implemented with service integrations
        pass
    
    @pytest.mark.external_service
    @pytest.mark.xfail(reason="Requires Active Directory integration - not yet implemented", strict=False)
    def test_execute_disable_user(self):
        """Test executing user disable."""
        mock_ad.disable_account.return_value = {"disabled": True}
        
        service = ApprovalService()
        action = {
            "type": "disable_user",
            "target": "compromised.user"
        }
        
        result = service.execute_action(action)
        
        assert result["disabled"] is True
    
    @pytest.mark.external_service
    @pytest.mark.xfail(reason="Requires CrowdStrike integration - not yet implemented", strict=False)
    def test_execute_action_error_handling(self):
        """Test error handling during action execution."""
        service = ApprovalService()
        
        action = {
            "type": "isolate_host",
            "target": "nonexistent-host"
        }
        
        # This test would require CrowdStrike service integration
        pytest.skip("CrowdStrike service not yet integrated")
        
        with patch('core.response.approval_service.CrowdStrikeService') as mock_cs:
            mock_cs.isolate_host.side_effect = Exception("Host not found")
            
            result = service.execute_action(action)
            
            assert result["status"] == "error"
            assert "Host not found" in result["error"]


@pytest.mark.external_service  # ApprovalService persists to PostgreSQL; no DB in the unit job
class TestApprovalQueue:
    """Test approval queue management."""
    
    def test_add_to_queue(self):
        """Test adding action to approval queue."""
        service = ApprovalService()
        
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.75,
            "reasoning": "Suspicious behavior"
        }
        
        action_id = service.add_to_queue(action)
        
        assert action_id is not None
        assert len(action_id) > 0
    
    def test_list_pending_approvals(self):
        """Test listing pending approvals."""
        service = ApprovalService()
        
        # Add multiple actions
        service.add_to_queue({"type": "isolate_host", "target": "host1", "confidence": 0.75})
        service.add_to_queue({"type": "block_ip", "target": "1.2.3.4", "confidence": 0.80})
        
        pending = service.list_pending_approvals()
        
        assert len(pending) >= 2
        assert all(a.status == "pending" for a in pending)
    
    def test_approve_action(self):
        """Test approving an action."""
        service = ApprovalService()
        
        action_id = service.add_to_queue({
            "type": "block_ip",
            "target": "1.2.3.4",
            "confidence": 0.75
        })
        
        result = service.approve_action(action_id, approved_by="analyst@company.com")
        
        assert result.status == "approved"
        assert result.approved_by == "analyst@company.com"
    
    def test_reject_action(self):
        """Test rejecting an action."""
        service = ApprovalService()
        
        action_id = service.add_to_queue({
            "type": "disable_user",
            "target": "user@company.com",
            "confidence": 0.70
        })
        
        result = service.reject_action(
            action_id,
            rejected_by="manager@company.com",
            reason="False positive - legitimate user activity"
        )
        
        assert result.status == "rejected"
        assert result.approved_by == "manager@company.com"  # rejection uses approved_by field


class TestAuditTrail:
    """Test audit trail logging."""
    
    def test_log_approval_decision(self):
        """Test logging approval decision."""
        service = ApprovalService()
        
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.92
        }
        
        log_entry = service.log_approval_decision(
            action=action,
            decision="auto_approved",
            user="system"
        )
        
        assert log_entry["decision"] == "auto_approved"
        assert log_entry["user"] == "system"
        assert "timestamp" in log_entry
    
    def test_log_execution_result(self):
        """Test logging execution result."""
        service = ApprovalService()
        
        log_entry = service.log_execution(
            action_id="action-123",
            status="success",
            result={"isolated": True}
        )
        
        assert log_entry["action_id"] == "action-123"
        assert log_entry["status"] == "success"
    
    @pytest.mark.external_service  # reads the audit trail from PostgreSQL
    def test_get_audit_trail(self):
        """Test retrieving audit trail."""
        service = ApprovalService()
        
        action_id = service.add_to_queue({
            "type": "block_ip",
            "target": "1.2.3.4",
            "confidence": 0.75
        })
        
        service.approve_action(action_id, approved_by="analyst")
        
        trail = service.get_audit_trail(action_id)
        
        assert len(trail) > 0
        assert any(e["event"] == "created" for e in trail)
        assert any(e["event"] == "approved" for e in trail)


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
            "entity_context": {"src_ips": ["10.0.1.5"]}
        }
        
        correlation = service.correlate_alerts(tempo_flow_alert=tempo_flow_alert)
        
        # Check that correlation returns expected structure
        assert "confidence" in correlation
        assert "indicators" in correlation
        assert "evidence" in correlation
        assert correlation["confidence"] > 0
    
    @pytest.mark.external_service
    @pytest.mark.xfail(reason="Requires correlation confidence calculation - method not yet implemented", strict=False)
    def test_calculate_correlation_confidence(self):
        """Test calculating confidence from correlated alerts."""
        pytest.skip("calculate_correlation_confidence method not yet implemented")
        
        service = AutonomousResponseService()
        
        factors = {
            "multiple_sources": 3,        # 3 different sources
            "severity_critical": True,     # At least one critical
            "time_correlation": True,      # Within 10 minutes
            "common_entities": ["10.0.1.5", "workstation-042"]
        }
        
        confidence = service.calculate_correlation_confidence(factors)
        
        assert confidence >= 0.80
        assert confidence <= 1.0
    
    @pytest.mark.skip(reason="process_correlated_alerts method not yet implemented")
    def test_autonomous_response_high_confidence(self):
        """Test autonomous response for high confidence."""
        # This test will be enabled once process_correlated_alerts is implemented
        pass
    
    @pytest.mark.external_service
    @pytest.mark.xfail(reason="Requires alert processing - method not yet implemented", strict=False)
    def test_autonomous_response_requires_approval(self):
        """Test autonomous response that requires approval."""
        pytest.skip("process_correlated_alerts method not yet implemented")
        
        service = AutonomousResponseService()
        
        alerts = [
            {"source": "splunk", "severity": "medium", "confidence": 0.75}
        ]
        
        response = service.process_correlated_alerts(alerts)
        
        assert response["requires_approval"] is True


class TestDryRunMode:
    """Test dry run mode for testing."""
    
    def test_dry_run_no_execution(self):
        """Test dry run mode doesn't execute actions."""
        service = ApprovalService(dry_run=True)
        
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.95
        }
        
        result = service.execute_action(action)
        
        assert result["status"] == "dry_run"
        assert result["would_execute"] is True
        # Verify no actual execution occurred
    
    @pytest.mark.external_service
    @pytest.mark.xfail(reason="Requires execution integration - testing dry run logging", strict=False)
    def test_dry_run_logging(self):
        """Test dry run mode logs actions."""
        service = ApprovalService(dry_run=True)
        
        action = {
            "type": "block_ip",
            "target": "1.2.3.4",
            "confidence": 0.92
        }
        
        result = service.execute_action(action)
        log = service.get_dry_run_log()
        
        assert len(log) > 0
        assert log[-1]["action"]["target"] == "1.2.3.4"


@pytest.mark.unit
@pytest.mark.external_service  # full workflow persists to PostgreSQL
class TestApprovalWorkflowIntegration:
    """Integration tests for approval workflow."""
    
    def test_full_approval_workflow(self):
        """Test complete approval workflow from creation to execution."""
        service = ApprovalService()
        
        # 1. Create action
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.75,
            "reasoning": "Multiple suspicious activities"
        }
        
        # 2. Add to queue (requires approval due to confidence < 0.90)
        action_id = service.add_to_queue(action)
        assert action_id is not None
        
        # 3. List pending
        pending = service.list_pending_approvals()
        assert any(a.action_id == action_id for a in pending)
        
        # 4. Approve
        approval = service.approve_action(action_id, approved_by="analyst")
        assert approval.status == "approved"
        
        # 5. Execute (with mock)
        with patch.object(service, 'execute_action') as mock_execute:
            mock_execute.return_value = {"status": "success"}
            result = service.execute_approved_action(action_id)
            assert result["status"] == "success"
        
        # 6. Verify audit trail
        trail = service.get_audit_trail(action_id)
        assert len(trail) >= 3  # created, approved, executed

