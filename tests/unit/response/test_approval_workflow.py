"""Unit tests for the approval gate's confidence band and the correlation logic."""

import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

from core.config import get_settings
from core.response.approval_service import ActionStatus, ActionType, ApprovalService
from core.response.autonomous_response_service import AutonomousResponseService
from core.response.config import ResponseConfig


@pytest.fixture
def no_db():
    """Stand-in session and config store so ApprovalService never reaches
    PostgreSQL for the row _put_action inserts.
    The status the test reads is decided before the session is touched.
    """
    manager = MagicMock()

    @contextmanager
    def _scope():
        yield MagicMock()

    manager.session_scope = _scope
    with patch("core.response.approval_service.get_db_manager", return_value=manager):
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
    """A confidence never marks a row approved (#2918)."""

    def test_a_confidence_of_0_99_stays_pending(self):
        action = _create(ApprovalService(config=ResponseConfig()), confidence=0.99)
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True
        assert action.reason.endswith("effect_class=reversible")

    def test_default_threshold_holds_below_ninety(self):
        action = _create(ApprovalService(config=ResponseConfig()), confidence=0.87)
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True

    def test_raised_threshold_does_not_release_the_row(self):
        svc = ApprovalService(config=ResponseConfig(confidence_threshold=0.95))
        action = _create(svc, confidence=0.90, effect_class="reversible")
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

    def test_force_manual_wins_over_confidence(self, monkeypatch):
        monkeypatch.setenv("DAEMON_FORCE_APPROVAL", "true")
        svc = ApprovalService(config=ResponseConfig())
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

    def test_an_act_flag_does_not_approve(self):
        """The ceiling can say act. A confidence of 0.99 still waits (#2931)."""
        stored = {
            "act_reversible": True,
            "act_irreversible": True,
            "stopped": False,
            "four_eyes": False,
        }
        store = Mock()
        store.read_system_config.return_value = stored
        with patch(
            "core.storage.config_service.get_config_service", return_value=store
        ):
            action = _create(ApprovalService(config=ResponseConfig()), confidence=0.99)
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True

    def test_construction_never_reads_or_writes_the_flag(self):
        store = Mock()
        with patch(
            "core.storage.config_service.get_config_service", return_value=store
        ):
            ApprovalService(config=ResponseConfig())
        store.assert_not_called()
        store.set_system_config.assert_not_called()

    def test_a_locked_environment_forces_the_rule_and_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("DAEMON_AUTO_RESPONSE", "false")
        store = Mock()
        with patch(
            "core.storage.config_service.get_config_service", return_value=store
        ):
            action = _create(ApprovalService(config=ResponseConfig()), confidence=0.99)
        assert action.status == ActionStatus.PENDING.value
        assert action.reason.endswith("approval.force_manual_approval=True")
        store.set_system_config.assert_not_called()


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
