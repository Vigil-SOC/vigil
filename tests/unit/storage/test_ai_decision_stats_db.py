"""get_ai_decision_stats scopes every figure to the requested agent.

PostgreSQL-backed: the outcome breakdown is a GROUP BY across agents, so the
bug only shows with real rows from more than one agent.
"""

import uuid

import pytest

from core.storage.service import DatabaseService

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _decide(service: DatabaseService, agent_id: str, outcome: str) -> None:
    decision_id = f"dec-{uuid.uuid4().hex[:12]}"
    service.create_ai_decision(
        decision_id=decision_id,
        agent_id=agent_id,
        decision_type="triage",
        confidence_score=0.8,
        reasoning="test",
        recommended_action="escalate",
    )
    service.submit_ai_decision_feedback(
        decision_id=decision_id,
        human_reviewer="analyst",
        human_decision="agree",
        actual_outcome=outcome,
    )


def test_outcome_breakdown_is_scoped_to_the_requested_agent():
    service = DatabaseService()
    suffix = uuid.uuid4().hex[:8]
    agent_a = f"agent-a-{suffix}"
    agent_b = f"agent-b-{suffix}"

    _decide(service, agent_a, "true_positive")
    _decide(service, agent_a, "true_positive")
    _decide(service, agent_a, "false_positive")
    _decide(service, agent_b, "benign")
    _decide(service, agent_b, "false_positive")

    stats = service.get_ai_decision_stats(agent_id=agent_a)

    assert stats["total_decisions"] == 3
    assert stats["outcomes"] == {"true_positive": 2, "false_positive": 1}
    # Every decision here has an outcome, so the breakdown sums to the total.
    assert sum(stats["outcomes"].values()) == stats["total_decisions"]
