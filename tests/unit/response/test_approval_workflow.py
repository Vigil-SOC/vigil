"""Unit tests for the approval gate's confidence band and the correlation logic."""

import pytest
from contextlib import contextmanager
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

    def test_stored_flag_change_reaches_a_running_service(self):
        """Settings writes Assist/Act while the service is up; the next action reads it."""
        stored = {"enabled": False}
        store = Mock()
        store.get_system_config.side_effect = lambda key, default=None: stored
        with patch(
            "core.response.approval_service.get_config_service", return_value=store
        ):
            svc = ApprovalService(config=ResponseConfig())
            assert _create(svc, confidence=0.99).status == ActionStatus.APPROVED.value
            stored["enabled"] = True
            assert _create(svc, confidence=0.99).status == ActionStatus.PENDING.value

    def test_a_failed_read_keeps_the_last_stored_flag(self):
        reads = iter([{"enabled": True}, {"enabled": True}, None])
        store = Mock()
        store.get_system_config.side_effect = lambda key, default=None: next(reads)
        with patch(
            "core.response.approval_service.get_config_service", return_value=store
        ):
            svc = ApprovalService(config=ResponseConfig())
            assert _create(svc, confidence=0.99).status == ActionStatus.PENDING.value
            assert _create(svc, confidence=0.99).status == ActionStatus.PENDING.value

    def test_forcing_approval_in_process_leaves_the_stored_row(self):
        store = Mock()
        store.get_system_config.return_value = {"enabled": False}
        with patch(
            "core.response.approval_service.get_config_service", return_value=store
        ):
            svc = ApprovalService(config=ResponseConfig())
            svc.set_force_manual_approval(True)
            assert _create(svc, confidence=0.99).status == ActionStatus.PENDING.value
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
