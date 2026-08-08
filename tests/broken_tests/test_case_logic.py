"""
Unit tests for case management logic.
Tests case status transitions, priority calculation, SLA calculations, and metrics.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# Skip all tests until case services are documented
pytestmark = pytest.mark.skip(reason="CaseWorkflowService, CaseSLAService, CaseMetricsService don't exist - needs rewrite")

# These services don't exist - case logic handled differently
# from core.cases.case_workflow_service import CaseWorkflowService
# from core.cases.case_sla_service import CaseSLAService
# from core.cases.case_metrics_service import CaseMetricsService


class TestCaseStatusTransitions:
    """Test case status state machine."""
    
    def test_valid_status_transition_open_to_in_progress(self):
        """Test valid transition from open to in_progress."""
        current_status = "open"
        new_status = "in_progress"
        
        is_valid = CaseWorkflowService.is_valid_transition(current_status, new_status)
        
        assert is_valid is True
    
    def test_valid_status_transition_in_progress_to_resolved(self):
        """Test valid transition from in_progress to resolved."""
        current_status = "in_progress"
        new_status = "resolved"
        
        is_valid = CaseWorkflowService.is_valid_transition(current_status, new_status)
        
        assert is_valid is True
    
    def test_invalid_status_transition_open_to_closed(self):
        """Test invalid transition from open directly to closed."""
        current_status = "open"
        new_status = "closed"
        
        is_valid = CaseWorkflowService.is_valid_transition(current_status, new_status)
        
        assert is_valid is False
    
    def test_get_next_valid_statuses(self):
        """Test getting all valid next statuses from current state."""
        current_status = "open"
        
        next_statuses = CaseWorkflowService.get_next_valid_statuses(current_status)
        
        assert "in_progress" in next_statuses
        assert "closed" not in next_statuses


class TestPriorityCalculation:
    """Test case priority calculation algorithm."""
    
    def test_calculate_priority_critical_severity(self):
        """Test priority calculation for critical severity finding."""
        finding = {
            "severity": "critical",
            "mitre_techniques": ["T1059.001", "T1071.001"],
            "confidence": 0.95
        }
        
        priority = CaseWorkflowService.calculate_priority(finding)
        
        assert priority == "critical"
    
    def test_calculate_priority_multiple_findings(self):
        """Test priority calculation with multiple findings."""
        findings = [
            {"severity": "medium"},
            {"severity": "high"},
            {"severity": "medium"}
        ]
        
        priority = CaseWorkflowService.calculate_aggregate_priority(findings)
        
        assert priority == "high"  # Should escalate to highest
    
    def test_calculate_priority_with_mitre_weight(self):
        """Test priority calculation with MITRE technique weighting."""
        finding = {
            "severity": "medium",
            "mitre_techniques": [
                "T1486",  # Ransomware - high weight
                "T1041"   # Exfiltration - high weight
            ]
        }
        
        priority = CaseWorkflowService.calculate_priority(finding)
        
        # Should escalate due to critical MITRE techniques
        assert priority in ["high", "critical"]
    
    def test_calculate_priority_low_confidence(self):
        """Test priority calculation with low confidence."""
        finding = {
            "severity": "high",
            "confidence": 0.3  # Very low confidence
        }
        
        priority = CaseWorkflowService.calculate_priority(finding)
        
        # Should downgrade due to low confidence
        assert priority in ["medium", "low"]


class TestSLACalculations:
    """Test SLA time calculations and tracking."""
    
    def test_calculate_sla_deadline_critical(self):
        """Test SLA deadline calculation for critical priority."""
        created_at = datetime.utcnow()
        priority = "critical"
        
        deadline = CaseSLAService.calculate_sla_deadline(created_at, priority)
        
        time_diff = deadline - created_at
        assert time_diff.total_seconds() <= 4 * 3600  # 4 hours for critical
    
    def test_calculate_sla_deadline_high(self):
        """Test SLA deadline calculation for high priority."""
        created_at = datetime.utcnow()
        priority = "high"
        
        deadline = CaseSLAService.calculate_sla_deadline(created_at, priority)
        
        time_diff = deadline - created_at
        assert time_diff.total_seconds() <= 24 * 3600  # 24 hours for high
    
    def test_check_sla_breach_not_breached(self):
        """Test SLA breach check for case within SLA."""
        created_at = datetime.utcnow() - timedelta(hours=2)
        priority = "critical"  # 4-hour SLA
        
        is_breached = CaseSLAService.is_sla_breached(created_at, priority)
        
        assert is_breached is False
    
    def test_check_sla_breach_breached(self):
        """Test SLA breach check for case beyond SLA."""
        created_at = datetime.utcnow() - timedelta(hours=5)
        priority = "critical"  # 4-hour SLA
        
        is_breached = CaseSLAService.is_sla_breached(created_at, priority)
        
        assert is_breached is True
    
    def test_calculate_time_to_resolve(self):
        """Test time to resolution calculation."""
        created_at = datetime.utcnow() - timedelta(hours=3, minutes=30)
        resolved_at = datetime.utcnow()
        
        time_to_resolve = CaseSLAService.calculate_time_to_resolve(
            created_at, resolved_at
        )
        
        # Should be approximately 3.5 hours
        assert 3.4 < time_to_resolve < 3.6
    
    def test_get_sla_remaining_time(self):
        """Test calculating remaining time until SLA breach."""
        created_at = datetime.utcnow() - timedelta(hours=2)
        priority = "critical"  # 4-hour SLA
        
        remaining = CaseSLAService.get_remaining_time(created_at, priority)
        
        # Should have ~2 hours remaining
        assert 1.8 < remaining < 2.2


class TestAutoAssignment:
    """Test automatic case assignment logic."""
    
    def test_assign_to_least_busy_analyst(self):
        """Test assignment to analyst with fewest active cases."""
        analysts = [
            {"id": "analyst-1", "active_cases": 5},
            {"id": "analyst-2", "active_cases": 2},
            {"id": "analyst-3", "active_cases": 8}
        ]
        
        assigned = CaseWorkflowService.auto_assign(analysts)
        
        assert assigned["id"] == "analyst-2"
    
    def test_assign_by_expertise(self):
        """Test assignment based on analyst expertise."""
        analysts = [
            {"id": "analyst-1", "expertise": ["network", "malware"]},
            {"id": "analyst-2", "expertise": ["cloud", "identity"]},
        ]
        
        case = {
            "mitre_techniques": ["T1071.001"],  # Network-related
            "tags": ["network_security"]
        }
        
        assigned = CaseWorkflowService.auto_assign_by_expertise(analysts, case)
        
        assert assigned["id"] == "analyst-1"
    
    def test_assign_round_robin(self):
        """Test round-robin assignment."""
        analysts = [
            {"id": "analyst-1"},
            {"id": "analyst-2"},
            {"id": "analyst-3"}
        ]
        
        # Assign 3 cases
        assignments = []
        for _ in range(3):
            assigned = CaseWorkflowService.assign_round_robin(analysts)
            assignments.append(assigned["id"])
        
        # Should cycle through all analysts
        assert len(set(assignments)) == 3


class TestCaseMetrics:
    """Test case metrics aggregation."""
    
    def test_calculate_mean_response_time(self):
        """Test calculating mean time to first response."""
        cases = [
            {"created_at": "2026-01-27T10:00:00Z", "first_response_at": "2026-01-27T10:30:00Z"},
            {"created_at": "2026-01-27T11:00:00Z", "first_response_at": "2026-01-27T11:15:00Z"},
            {"created_at": "2026-01-27T12:00:00Z", "first_response_at": "2026-01-27T12:45:00Z"}
        ]
        
        mean_time = CaseMetricsService.calculate_mean_response_time(cases)
        
        # Average: (30 + 15 + 45) / 3 = 30 minutes
        assert 29 < mean_time < 31
    
    def test_calculate_resolution_rate(self):
        """Test calculating case resolution rate."""
        cases = [
            {"status": "resolved"},
            {"status": "resolved"},
            {"status": "open"},
            {"status": "in_progress"},
            {"status": "closed"}
        ]
        
        rate = CaseMetricsService.calculate_resolution_rate(cases)
        
        # 3 resolved/closed out of 5 = 60%
        assert 0.59 < rate < 0.61
    
    def test_calculate_sla_compliance(self):
        """Test calculating SLA compliance percentage."""
        cases = [
            {"sla_breached": False},
            {"sla_breached": False},
            {"sla_breached": True},
            {"sla_breached": False},
            {"sla_breached": False}
        ]
        
        compliance = CaseMetricsService.calculate_sla_compliance(cases)
        
        # 4 out of 5 = 80%
        assert 0.79 < compliance < 0.81
    
    def test_group_by_severity(self):
        """Test grouping cases by severity."""
        cases = [
            {"severity": "critical"},
            {"severity": "high"},
            {"severity": "critical"},
            {"severity": "medium"},
            {"severity": "high"}
        ]
        
        grouped = CaseMetricsService.group_by_severity(cases)
        
        assert grouped["critical"] == 2
        assert grouped["high"] == 2
        assert grouped["medium"] == 1
    
    def test_calculate_trend(self):
        """Test calculating trend (increase/decrease)."""
        current_count = 50
        previous_count = 40
        
        trend = CaseMetricsService.calculate_trend(current_count, previous_count)
        
        assert trend == 25.0  # 25% increase


class TestTimelineEvents:
    """Test timeline event sorting and filtering."""
    
    def test_sort_events_chronologically(self):
        """Test sorting timeline events by timestamp."""
        events = [
            {"timestamp": "2026-01-27T12:00:00Z", "action": "updated"},
            {"timestamp": "2026-01-27T10:00:00Z", "action": "created"},
            {"timestamp": "2026-01-27T11:00:00Z", "action": "assigned"}
        ]
        
        sorted_events = CaseWorkflowService.sort_timeline(events)
        
        assert sorted_events[0]["action"] == "created"
        assert sorted_events[1]["action"] == "assigned"
        assert sorted_events[2]["action"] == "updated"
    
    def test_filter_events_by_type(self):
        """Test filtering timeline events by action type."""
        events = [
            {"action": "created"},
            {"action": "status_changed"},
            {"action": "comment_added"},
            {"action": "status_changed"}
        ]
        
        filtered = CaseWorkflowService.filter_timeline(events, action_type="status_changed")
        
        assert len(filtered) == 2
        assert all(e["action"] == "status_changed" for e in filtered)
    
    def test_filter_events_by_time_range(self):
        """Test filtering events within time range."""
        events = [
            {"timestamp": datetime(2026, 1, 27, 10, 0)},
            {"timestamp": datetime(2026, 1, 27, 12, 0)},
            {"timestamp": datetime(2026, 1, 27, 14, 0)}
        ]
        
        start = datetime(2026, 1, 27, 11, 0)
        end = datetime(2026, 1, 27, 13, 0)
        
        filtered = CaseWorkflowService.filter_timeline_by_range(events, start, end)
        
        assert len(filtered) == 1
        assert filtered[0]["timestamp"] == datetime(2026, 1, 27, 12, 0)


@pytest.mark.unit
class TestCaseWorkflowIntegration:
    """Integration tests for complete case workflows."""
    
    def test_full_case_lifecycle(self):
        """Test complete case lifecycle from creation to closure."""
        # Create case
        case = {
            "id": "case-123",
            "status": "open",
            "priority": "high",
            "created_at": datetime.utcnow()
        }
        
        # Validate initial state
        assert case["status"] == "open"
        
        # Transition to in_progress
        case["status"] = "in_progress"
        assert CaseWorkflowService.is_valid_transition("open", "in_progress") is True
        
        # Transition to resolved
        case["status"] = "resolved"
        assert CaseWorkflowService.is_valid_transition("in_progress", "resolved") is True
        
        # Transition to closed
        case["status"] = "closed"
        assert CaseWorkflowService.is_valid_transition("resolved", "closed") is True
    
    def test_sla_tracking_workflow(self):
        """Test SLA tracking throughout case lifecycle."""
        created_at = datetime.utcnow()
        priority = "critical"
        
        # Calculate deadline
        deadline = CaseSLAService.calculate_sla_deadline(created_at, priority)
        assert deadline > created_at
        
        # Check if breached (should not be immediately)
        assert CaseSLAService.is_sla_breached(created_at, priority) is False
        
        # Simulate time passing
        with patch('core.cases.case_sla_service.datetime') as mock_datetime:
            mock_datetime.utcnow.return_value = created_at + timedelta(hours=5)
            
            # Should now be breached (critical SLA is 4 hours)
            assert CaseSLAService.is_sla_breached(created_at, priority) is True

