"""The pending-feedback queue can be narrowed to one agent, and a decision the
model gave no confidence for sorts ahead of the rest."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.reporting import ai_decisions_router

pytestmark = pytest.mark.unit


def _decision(decision_id: str, confidence):
    return SimpleNamespace(
        id=1,
        decision_id=decision_id,
        agent_id="triage",
        workflow_id=None,
        finding_id=None,
        case_id=None,
        decision_type="triage",
        confidence_score=confidence,
        reasoning="",
        recommended_action="monitor",
        decision_metadata=None,
        human_reviewer=None,
        human_decision=None,
        feedback_comment=None,
        accuracy_grade=None,
        reasoning_grade=None,
        action_appropriateness=None,
        actual_outcome=None,
        time_saved_minutes=None,
        timestamp=datetime(2026, 10, 13, 12, 0),
        feedback_timestamp=None,
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(
        ai_decisions_router.router, prefix=ai_decisions_router.ROUTER_META.prefix
    )
    return TestClient(app)


def test_the_agent_filter_reaches_the_query_and_none_sorts_first(client):
    rows = [_decision("a", 0.9), _decision("b", None), _decision("c", 0.2)]
    with patch.object(ai_decisions_router, "DatabaseService") as service:
        service.return_value.list_ai_decisions.return_value = rows
        res = client.get("/api/ai/decisions/pending-feedback?agent_id=triage")

    assert res.status_code == 200
    service.return_value.list_ai_decisions.assert_called_once_with(
        agent_id="triage", has_feedback=False, limit=50
    )
    body = res.json()
    assert [d["decision_id"] for d in body] == ["b", "c", "a"]
    assert body[0]["confidence_score"] is None


def test_without_a_filter_every_agent_is_queued(client):
    with patch.object(ai_decisions_router, "DatabaseService") as service:
        service.return_value.list_ai_decisions.return_value = []
        assert client.get("/api/ai/decisions/pending-feedback").status_code == 200
    service.return_value.list_ai_decisions.assert_called_once_with(
        agent_id=None, has_feedback=False, limit=50
    )
