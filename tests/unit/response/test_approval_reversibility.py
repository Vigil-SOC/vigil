"""Reversibility gate and idempotent create on ApprovalAction (#827)."""

from __future__ import annotations

import pytest

from core.response.approval_service import (
    ActionStatus,
    ActionType,
    ApprovalService,
    Reversibility,
)
from core.response.autonomous_response_service import AutonomousResponseService

pytestmark = pytest.mark.external_service


def _create(
    svc: ApprovalService,
    *,
    confidence: float,
    reversibility: Reversibility = Reversibility.REVERSIBLE,
    idempotency_key: str | None = None,
    target: str = "10.0.1.5",
    action_type: ActionType = ActionType.ISOLATE_HOST,
):
    svc.force_manual_approval = False
    return svc.create_action(
        action_type=action_type,
        title=f"{action_type.value}: {target}",
        description="test",
        target=target,
        confidence=confidence,
        reason="test",
        evidence=["ev-1"],
        created_by="pytest",
        reversibility=reversibility,
        idempotency_key=idempotency_key,
    )


class TestReversibilityGate:
    def test_irreversible_high_confidence_stays_pending(self):
        action = _create(
            ApprovalService(),
            confidence=0.99,
            reversibility=Reversibility.IRREVERSIBLE,
        )
        assert action.status == ActionStatus.PENDING.value
        assert action.requires_approval is True
        assert action.reversibility == Reversibility.IRREVERSIBLE.value
        assert action.reason == "test; reversibility=irreversible"

    def test_reversible_high_confidence_still_auto_approves(self):
        svc = ApprovalService()
        action = _create(svc, confidence=0.95, reversibility=Reversibility.REVERSIBLE)
        assert action.status == ActionStatus.APPROVED.value
        assert action.requires_approval is False
        assert action.reversibility == Reversibility.REVERSIBLE.value
        threshold = svc.config.confidence_threshold
        assert action.reason == (
            f"test; response.confidence_threshold={threshold:.2f} met (0.95)"
        )

    def test_forced_approval_records_the_force_switch(self):
        svc = ApprovalService()
        action = _create(svc, confidence=0.95)
        assert action.requires_approval is False
        svc.force_manual_approval = True
        forced = svc.create_action(
            action_type=ActionType.ISOLATE_HOST,
            title="isolate_host: 10.0.1.6",
            description="test",
            target="10.0.1.6",
            confidence=0.95,
            reason="test",
            evidence=["ev-1"],
        )
        assert forced.requires_approval is True
        assert forced.reason == "test; approval.force_manual_approval=True"


class TestIdempotencyKey:
    def test_same_key_returns_existing_row(self):
        svc = ApprovalService()
        first = _create(svc, confidence=0.5, idempotency_key="isolate_host:10.0.1.5")
        second = _create(svc, confidence=0.5, idempotency_key="isolate_host:10.0.1.5")
        assert second.action_id == first.action_id
        listed = svc.list_actions()
        matching = [a for a in listed if a.idempotency_key == "isolate_host:10.0.1.5"]
        assert len(matching) == 1

    def test_failed_row_may_be_retried(self):
        svc = ApprovalService()
        first = _create(svc, confidence=0.5, idempotency_key="retry-me")
        failed = svc.mark_failed(first.action_id, "containment timed out")
        assert failed is not None
        assert failed.status == ActionStatus.FAILED.value
        second = _create(svc, confidence=0.5, idempotency_key="retry-me")
        assert second.action_id != first.action_id
        assert second.status == ActionStatus.PENDING.value


class TestIsolationIdempotency:
    def test_same_host_is_not_executed_twice(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        executions: list[str] = []

        def _fake_execute(ip_address, hostname, reason, confidence):
            executions.append(ip_address)
            return {"success": True, "ip_address": ip_address}

        response._execute_isolation = _fake_execute  # type: ignore[method-assign]

        first = response.create_isolation_action(
            ip_address="10.0.9.9",
            hostname="ws-9",
            confidence=0.95,
            reason="c2",
            evidence=["ev-1"],
            correlation_data={
                "indicators": ["c2_communication"],
                "reasoning": ["beacon"],
            },
        )
        second = response.create_isolation_action(
            ip_address="10.0.9.9",
            hostname="ws-9",
            confidence=0.95,
            reason="c2",
            evidence=["ev-1"],
            correlation_data={
                "indicators": ["c2_communication"],
                "reasoning": ["beacon"],
            },
        )

        assert first["status"] == "executed"
        assert second["status"] == "executed"
        assert first["action_id"] == second["action_id"]
        assert executions == ["10.0.9.9"]
        action = response.approval_service.get_action(first["action_id"])
        assert action is not None
        assert action.idempotency_key == "isolate_host:10.0.9.9"
        assert action.status == ActionStatus.EXECUTED.value

    def test_rejected_isolate_is_not_executed_on_retry(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        executions: list[str] = []

        def _fake_execute(ip_address, hostname, reason, confidence):
            executions.append(ip_address)
            return {"success": True, "ip_address": ip_address}

        response._execute_isolation = _fake_execute  # type: ignore[method-assign]

        first = response.create_isolation_action(
            ip_address="10.0.8.8",
            hostname="ws-8",
            confidence=0.70,
            reason="c2",
            evidence=["ev-1"],
            correlation_data={
                "indicators": ["c2_communication"],
                "reasoning": ["beacon"],
            },
        )
        assert first["status"] == "pending_approval"
        rejected = response.approval_service.reject_action(
            first["action_id"], reason="false positive"
        )
        assert rejected is not None
        assert rejected.status == ActionStatus.REJECTED.value

        second = response.create_isolation_action(
            ip_address="10.0.8.8",
            hostname="ws-8",
            confidence=0.70,
            reason="c2",
            evidence=["ev-1"],
            correlation_data={
                "indicators": ["c2_communication"],
                "reasoning": ["beacon"],
            },
        )
        assert second["action_id"] == first["action_id"]
        assert second["status"] == ActionStatus.REJECTED.value
        assert executions == []


class TestIpLessIsolationKey:
    """#1217: an IP-less finding must not collide on the literal "unknown" key."""

    def test_different_hostnames_with_no_ip_get_distinct_rows(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        executions: list[str] = []

        def _fake_execute(ip_address, hostname, reason, confidence):
            executions.append(hostname)
            return {"success": True, "hostname": hostname}

        response._execute_isolation = _fake_execute  # type: ignore[method-assign]

        first = response.create_isolation_action(
            ip_address="unknown",
            hostname="host-a",
            confidence=0.95,
            reason="hids",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )
        second = response.create_isolation_action(
            ip_address="unknown",
            hostname="host-b",
            confidence=0.95,
            reason="hids",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )

        assert first["status"] == "executed" and not first.get("reused")
        assert second["status"] == "executed" and not second.get("reused")
        assert first["action_id"] != second["action_id"]
        assert executions == ["host-a", "host-b"]

    def test_same_hostname_with_no_ip_reuses_one_row(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        executions: list[str] = []

        def _fake_execute(ip_address, hostname, reason, confidence):
            executions.append(hostname)
            return {"success": True, "hostname": hostname}

        response._execute_isolation = _fake_execute  # type: ignore[method-assign]

        first = response.create_isolation_action(
            ip_address="unknown",
            hostname="host-c",
            confidence=0.95,
            reason="hids",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )
        second = response.create_isolation_action(
            ip_address="unknown",
            hostname="host-c",
            confidence=0.95,
            reason="hids",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )

        assert first["action_id"] == second["action_id"]
        assert not first.get("reused")
        assert second["status"] == "executed" and second.get("reused") is True
        assert executions == ["host-c"]
        action = response.approval_service.get_action(first["action_id"])
        assert action is not None
        assert action.idempotency_key == "isolate_host:host:host-c"


class TestUnwiredIsolation:
    """#1276: the isolation stub must not record a containment that never ran."""

    def test_auto_approved_isolation_is_recorded_failed(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        result = response.create_isolation_action(
            ip_address="10.0.0.5",
            hostname="host-a",
            confidence=0.95,
            reason="test",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )
        assert result["status"] == ActionStatus.FAILED.value
        assert result["result"]["success"] is False
        assert result["result"]["error"] == "unsupported_action_type"
        action = response.approval_service.get_action(result["action_id"])
        assert action is not None
        assert action.status == ActionStatus.FAILED.value
        assert action.execution_result == {"error": "unsupported_action_type"}

        retry = response.create_isolation_action(
            ip_address="10.0.0.5",
            hostname="host-a",
            confidence=0.95,
            reason="test",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )
        assert retry["action_id"] != result["action_id"]
        assert retry["status"] == ActionStatus.FAILED.value

    def test_analyst_approved_isolation_is_recorded_failed(self):
        response = AutonomousResponseService()
        response.approval_service.force_manual_approval = False
        created = response.create_isolation_action(
            ip_address="10.0.0.6",
            hostname="host-b",
            confidence=0.50,
            reason="test",
            evidence=["ev-1"],
            correlation_data={"indicators": [], "reasoning": []},
        )
        assert created["status"] == "pending_approval"
        approved = response.approval_service.approve_action(created["action_id"])
        assert approved is not None
        assert approved.status == ActionStatus.APPROVED.value

        results = response.execute_approved_actions()
        match = [r for r in results if r["action_id"] == created["action_id"]]
        assert len(match) == 1
        assert match[0]["result"]["success"] is False
        assert match[0]["result"]["error"] == "unsupported_action_type"
        action = response.approval_service.get_action(created["action_id"])
        assert action is not None
        assert action.status == ActionStatus.FAILED.value
        assert action.execution_result == {"error": "unsupported_action_type"}
