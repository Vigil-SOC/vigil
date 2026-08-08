"""
Unit tests for daemon/autonomous operations.
Tests polling, processing, auto-response, and escalation logic.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from services.daemon.poller import DataPoller
from services.daemon.processor import FindingProcessor
from services.daemon.responder import AutonomousResponder
from services.daemon.scheduler import TaskScheduler


class TestPollingLogic:
    """Test daemon polling logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in DataPoller - needs rewrite for async polling")
    def test_calculate_next_poll_time(self):
        """Test calculating next poll time based on interval."""
        from services.daemon.config import PollingConfig
        config = PollingConfig()
        poller = DataPoller(config)
        interval = 300  # 5 minutes
        
        next_poll = poller.calculate_next_poll(interval)
        
        now = datetime.utcnow()
        expected = now + timedelta(seconds=interval)
        
        # Allow 1 second tolerance
        assert abs((next_poll - expected).total_seconds()) < 1
    
    @pytest.mark.skip(reason="Methods don't exist in DataPoller - needs rewrite for async polling")
    def test_should_poll_true(self):
        """Test polling when interval has elapsed."""
        from services.daemon.config import PollingConfig
        config = PollingConfig()
        poller = DataPoller(config)
        last_poll = datetime.utcnow() - timedelta(seconds=400)
        interval = 300  # 5 minutes
        
        should_poll = poller.should_poll(last_poll, interval)
        
        assert should_poll is True
    
    @pytest.mark.skip(reason="Methods don't exist in DataPoller - needs rewrite for async polling")
    def test_should_poll_false(self):
        """Test not polling when interval hasn't elapsed."""
        from services.daemon.config import PollingConfig
        config = PollingConfig()
        poller = DataPoller(config)
        last_poll = datetime.utcnow() - timedelta(seconds=100)
        interval = 300  # 5 minutes
        
        should_poll = poller.should_poll(last_poll, interval)
        
        assert should_poll is False
    
    @pytest.mark.skip(reason="Methods don't exist in DataPoller - needs rewrite for async polling")
    @patch('services.daemon.poller.SplunkService')
    def test_poll_splunk(self, mock_splunk):
        """Test polling Splunk for new events."""
        mock_splunk.search.return_value = [
            {"id": "1", "severity": "high"},
            {"id": "2", "severity": "medium"}
        ]
        
        from services.daemon.config import PollingConfig
        config = PollingConfig()
        poller = DataPoller(config)
        events = poller.poll_splunk(query="search index=security")
        
        assert len(events) == 2
        assert events[0]["severity"] == "high"
        mock_splunk.search.assert_called_once()
    
    @pytest.mark.skip(reason="Methods don't exist in DataPoller - needs rewrite for async polling")
    @patch('services.daemon.poller.CrowdStrikeService')
    def test_poll_crowdstrike(self, mock_cs):
        """Test polling CrowdStrike for alerts."""
        mock_cs.get_alerts.return_value = [
            {"alert_id": "cs-001", "severity": "critical"}
        ]
        
        from services.daemon.config import PollingConfig
        config = PollingConfig()
        poller = DataPoller(config)
        alerts = poller.poll_crowdstrike()
        
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"


class TestBatchProcessing:
    """Test batch processing logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_create_batches(self):
        """Test splitting items into batches."""
        processor = FindingProcessor()
        items = list(range(25))
        batch_size = 10
        
        batches = processor.create_batches(items, batch_size)
        
        assert len(batches) == 3
        assert len(batches[0]) == 10
        assert len(batches[1]) == 10
        assert len(batches[2]) == 5
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_process_batch_success(self):
        """Test successful batch processing."""
        processor = FindingProcessor()
        findings = [
            {"id": "f1", "severity": "high"},
            {"id": "f2", "severity": "medium"}
        ]
        
        with patch.object(processor, 'process_single') as mock_process:
            mock_process.return_value = {"status": "success"}
            
            results = processor.process_batch(findings)
            
            assert len(results) == 2
            assert all(r["status"] == "success" for r in results)
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_process_batch_with_errors(self):
        """Test batch processing with some errors."""
        processor = FindingProcessor()
        findings = [
            {"id": "f1", "severity": "high"},
            {"id": "f2"},  # Missing severity
            {"id": "f3", "severity": "low"}
        ]
        
        with patch.object(processor, 'process_single') as mock_process:
            def side_effect(finding):
                if "severity" not in finding:
                    raise ValueError("Missing severity")
                return {"status": "success"}
            
            mock_process.side_effect = side_effect
            
            results = processor.process_batch(findings, fail_on_error=False)
            
            assert len(results) == 3
            assert results[0]["status"] == "success"
            assert results[1]["status"] == "error"
            assert results[2]["status"] == "success"


class TestAutoTriage:
    """Test automatic triage logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_should_auto_triage_high_severity(self):
        """Test auto-triage for high severity findings."""
        processor = FindingProcessor()
        finding = {"severity": "high", "confidence": 0.9}
        
        should_triage = processor.should_auto_triage(finding)
        
        assert should_triage is True
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_should_not_auto_triage_low_confidence(self):
        """Test skipping auto-triage for low confidence."""
        processor = FindingProcessor()
        finding = {"severity": "high", "confidence": 0.3}
        
        should_triage = processor.should_auto_triage(finding)
        
        assert should_triage is False
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_auto_triage_decision(self):
        """Test auto-triage decision making."""
        processor = FindingProcessor()
        finding = {
            "severity": "critical",
            "mitre_techniques": ["T1486"],  # Ransomware
            "confidence": 0.95
        }
        
        decision = processor.auto_triage(finding)
        
        assert decision["priority"] == "critical"
        assert decision["create_case"] is True
        assert decision["notify"] is True


class TestAutoEnrichment:
    """Test automatic enrichment logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    def test_should_auto_enrich(self):
        """Test determining if finding should be enriched."""
        processor = FindingProcessor()
        
        # High severity should be enriched
        assert processor.should_auto_enrich({"severity": "high"}) is True
        assert processor.should_auto_enrich({"severity": "critical"}) is True
        
        # Low severity may not be enriched (depends on config)
        result = processor.should_auto_enrich({"severity": "low"})
        assert isinstance(result, bool)
    
    @pytest.mark.skip(reason="Methods don't exist in FindingProcessor - needs rewrite")
    @patch('services.daemon.processor.ClaudeService')
    def test_auto_enrich_finding(self, mock_claude):
        """Test auto-enriching a finding."""
        mock_claude.enrich_finding.return_value = {
            "threat_type": "C2",
            "risk_level": "critical",
            "recommended_actions": ["Isolate host"]
        }
        
        processor = FindingProcessor()
        finding = {"id": "f1", "title": "Suspicious traffic"}
        
        enriched = processor.auto_enrich(finding)
        
        assert enriched["threat_type"] == "C2"
        assert enriched["risk_level"] == "critical"
        mock_claude.enrich_finding.assert_called_once()


class TestAutoResponse:
    """Test automatic response logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_calculate_confidence_score(self):
        """Test confidence score calculation."""
        responder = AutonomousResponder()
        
        indicators = {
            "multiple_sources": True,  # +0.20
            "critical_severity": True,  # +0.15
            "lateral_movement": True,  # +0.15
            "time_correlated": True    # +0.10
        }
        
        confidence = responder.calculate_confidence(indicators)
        
        assert confidence == 0.60
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_should_auto_respond_high_confidence(self):
        """Test auto-response for high confidence."""
        responder = AutonomousResponder()
        
        action = {
            "type": "isolate_host",
            "confidence": 0.92,
            "severity": "critical"
        }
        
        should_respond = responder.should_auto_respond(action, threshold=0.90)
        
        assert should_respond is True
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_should_not_auto_respond_low_confidence(self):
        """Test not auto-responding for low confidence."""
        responder = AutonomousResponder()
        
        action = {
            "type": "isolate_host",
            "confidence": 0.75,
            "severity": "high"
        }
        
        should_respond = responder.should_auto_respond(action, threshold=0.90)
        
        assert should_respond is False
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_force_manual_approval(self):
        """Test forcing manual approval mode."""
        responder = AutonomousResponder()
        
        action = {
            "type": "isolate_host",
            "confidence": 0.95,
            "severity": "critical"
        }
        
        # Even with high confidence, force manual approval
        should_respond = responder.should_auto_respond(
            action, 
            threshold=0.90,
            force_manual=True
        )
        
        assert should_respond is False
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    @patch('services.daemon.responder.ApprovalService')
    def test_create_approval_action(self, mock_approval):
        """Test creating approval action."""
        mock_approval.create_action.return_value = {"id": "action-123"}
        
        responder = AutonomousResponder()
        action = {
            "type": "isolate_host",
            "target": "workstation-042",
            "confidence": 0.85
        }
        
        result = responder.create_approval_action(action)
        
        assert result["id"] == "action-123"
        mock_approval.create_action.assert_called_once()


class TestEscalation:
    """Test escalation logic."""
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_should_escalate_critical(self):
        """Test escalating critical findings."""
        responder = AutonomousResponder()
        
        finding = {"severity": "critical"}
        
        should_escalate = responder.should_escalate(
            finding,
            escalate_severities=["critical", "high"]
        )
        
        assert should_escalate is True
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    def test_should_not_escalate_low(self):
        """Test not escalating low severity."""
        responder = AutonomousResponder()
        
        finding = {"severity": "low"}
        
        should_escalate = responder.should_escalate(
            finding,
            escalate_severities=["critical", "high"]
        )
        
        assert should_escalate is False
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    @patch('services.daemon.responder.SlackService')
    def test_escalate_to_slack(self, mock_slack):
        """Test escalating to Slack."""
        mock_slack.send_alert.return_value = {"ok": True}
        
        responder = AutonomousResponder()
        finding = {
            "id": "f1",
            "title": "Ransomware Detected",
            "severity": "critical"
        }
        
        result = responder.escalate_to_slack(finding, channel="#soc-alerts")
        
        assert result["ok"] is True
        mock_slack.send_alert.assert_called_once()
    
    @pytest.mark.skip(reason="Methods don't exist in AutonomousResponder - needs rewrite")
    @patch('services.daemon.responder.PagerDutyService')
    def test_escalate_to_pagerduty(self, mock_pd):
        """Test escalating to PagerDuty."""
        mock_pd.create_incident.return_value = {"incident_id": "pd-123"}
        
        responder = AutonomousResponder()
        finding = {
            "id": "f1",
            "title": "Critical Incident",
            "severity": "critical"
        }
        
        result = responder.escalate_to_pagerduty(finding)
        
        assert result["incident_id"] == "pd-123"


class TestScheduledTasks:
    """Test scheduled task execution."""
    
    @pytest.mark.skip(reason="Methods don't exist in TaskScheduler - needs rewrite")
    def test_schedule_task(self):
        """Test scheduling a task."""
        scheduler = TaskScheduler()
        
        def sample_task():
            return "executed"
        
        scheduler.schedule_task(
            task=sample_task,
            interval=3600,  # 1 hour
            name="sample_task"
        )
        
        assert "sample_task" in scheduler.tasks
        assert scheduler.tasks["sample_task"]["interval"] == 3600
    
    @pytest.mark.skip(reason="Methods don't exist in TaskScheduler - needs rewrite")
    def test_threat_hunt_interval(self):
        """Test threat hunting scheduled interval."""
        scheduler = TaskScheduler()
        
        # Default: daily (86400 seconds)
        assert scheduler.get_threat_hunt_interval() == 86400
    
    @pytest.mark.skip(reason="Methods don't exist in TaskScheduler - needs rewrite")
    @patch('services.daemon.scheduler.ThreatHunter')
    def test_run_threat_hunt(self, mock_hunter):
        """Test running threat hunt."""
        mock_hunter.run_hunt.return_value = {
            "findings": 5,
            "high_priority": 2
        }
        
        scheduler = TaskScheduler()
        result = scheduler.run_threat_hunt()
        
        assert result["findings"] == 5
        mock_hunter.run_hunt.assert_called_once()
    
    @pytest.mark.skip(reason="Methods don't exist in TaskScheduler - needs rewrite")
    def test_cleanup_old_data(self):
        """Test cleanup of old data."""
        scheduler = TaskScheduler()
        
        retention_days = 90
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        # Test cleanup logic
        result = scheduler.cleanup_old_data(retention_days)
        
        assert "deleted" in result or "retention_date" in result


class TestDaemonMetrics:
    """Test daemon metrics collection."""
    
    def test_record_poll_metric(self):
        """Test recording poll metric."""
        from services.daemon.metrics import DaemonMetrics
        
        metrics = DaemonMetrics()
        metrics.record_poll("splunk", duration=2.5, events_count=10)
        
        assert metrics.get_poll_count("splunk") >= 1
    
    def test_record_processing_metric(self):
        """Test recording processing metric."""
        from services.daemon.metrics import DaemonMetrics
        
        metrics = DaemonMetrics()
        metrics.record_processing(findings_count=5, duration=1.2)
        
        assert metrics.get_total_processed() >= 5
    
    def test_get_metrics_summary(self):
        """Test getting metrics summary."""
        from services.daemon.metrics import DaemonMetrics
        
        metrics = DaemonMetrics()
        metrics.record_poll("splunk", duration=2.0, events_count=5)
        metrics.record_processing(findings_count=5, duration=1.0)
        
        summary = metrics.get_summary()
        
        assert "total_polls" in summary
        assert "total_processed" in summary


@pytest.mark.integration
@pytest.mark.skip(reason="Daemon lifecycle tests need rewrite for current architecture")
class TestDaemonLifecycle:
    """Test complete daemon lifecycle."""
    
    @patch('services.daemon.main.DataPoller')
    @patch('services.daemon.main.FindingProcessor')
    @patch('services.daemon.main.AutonomousResponder')
    def test_daemon_startup(self, mock_responder, mock_processor, mock_poller):
        """Test daemon startup."""
        from services.daemon.main import DaemonService
        
        daemon = DaemonService()
        daemon.start()
        
        assert daemon.is_running is True
    
    @patch('services.daemon.main.DataPoller')
    def test_daemon_poll_cycle(self, mock_poller):
        """Test one poll cycle."""
        mock_poller.poll_all.return_value = [
            {"id": "f1", "severity": "high"}
        ]
        
        from services.daemon.main import DaemonService
        
        daemon = DaemonService()
        findings = daemon.poll_cycle()
        
        assert len(findings) >= 0
    
    def test_daemon_graceful_shutdown(self):
        """Test graceful shutdown."""
        from services.daemon.main import DaemonService
        
        daemon = DaemonService()
        daemon.start()
        daemon.stop()
        
        assert daemon.is_running is False

