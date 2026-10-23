"""Feedback on an AI decision names the signed-in user and takes the approvals right.

The reviewer used to be the ``human_reviewer`` the request body carried, so any
login could grade a decision in anyone's name. These run against the mounted app
so the router's auth dependency and the permission gate are the real ones; the
database is a stub.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from core.auth import current_user as current_user_module
from core.reporting import ai_decisions_router
from core.routing import request_unit_of_work
from core.storage.models import AIDecisionLog, User
from services.api import main as backend_main
from services.api.middleware import auth as auth_module

pytestmark = pytest.mark.unit

FEEDBACK = "/api/ai/decisions/dec-1/feedback"


class StubDecisions:
    def __init__(self):
        self.feedback = []

    def submit_ai_decision_feedback(self, decision_id, **fields):
        self.feedback.append((decision_id, fields))
        return AIDecisionLog(
            id=1,
            decision_id=decision_id,
            agent_id="triage",
            decision_type="triage",
            confidence_score=0.8,
            reasoning="r",
            recommended_action="escalate",
            timestamp=datetime(2026, 10, 13),
            feedback_timestamp=datetime(2026, 10, 13),
            **fields,
        )

    def get_ai_decision_stats(self, agent_id=None, days=30):
        return {
            "total_decisions": 4,
            "total_with_feedback": 2,
            "feedback_rate": 50.0,
            "agreement_rate": 100.0,
            "avg_accuracy_grade": 0.8,
            "outcomes": {"true_positive": 2},
            "total_time_saved_minutes": 30,
            "total_time_saved_hours": 0.5,
            "period_days": days,
        }


@pytest.fixture()
def decisions(monkeypatch):
    stub = StubDecisions()
    monkeypatch.setattr(ai_decisions_router, "DatabaseService", lambda: stub)
    return stub


@pytest.fixture()
def viewer(monkeypatch):
    """Signed in, holding every permission except ai_decisions.approve."""
    user = User(
        user_id="viewer",
        principal_id="viewer",
        username="vera_viewer",
        email="v@test.local",
        password_hash="",
        role_id="role-viewer",
        is_active=True,
        mfa_enabled=False,
    )
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda user_id, perm, session=None: perm != "ai_decisions.approve",
    )
    app = backend_main.app
    app.dependency_overrides[auth_module.get_current_active_user] = lambda: user
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.pop(auth_module.get_current_active_user, None)
    app.dependency_overrides.pop(auth_module.get_current_user, None)


def test_the_session_user_is_recorded_and_a_body_reviewer_is_ignored(
    authenticate_app, decisions
):
    authenticate_app(backend_main.app)

    resp = TestClient(backend_main.app).post(
        FEEDBACK,
        json={"human_reviewer": "mallory", "human_decision": "disagree"},
    )

    assert resp.status_code == 200, resp.text
    [(decision_id, fields)] = decisions.feedback
    assert decision_id == "dec-1"
    assert fields["human_reviewer"] == "test-admin"
    assert resp.json()["human_reviewer"] == "test-admin"


def test_feedback_without_a_reviewer_field_is_accepted(authenticate_app, decisions):
    authenticate_app(backend_main.app)

    resp = TestClient(backend_main.app).post(FEEDBACK, json={"human_decision": "agree"})

    assert resp.status_code == 200, resp.text
    assert decisions.feedback[0][1]["human_reviewer"] == "test-admin"


def test_a_user_without_the_approvals_right_cannot_grade(viewer, decisions):
    resp = viewer.post(FEEDBACK, json={"human_decision": "agree"})

    assert resp.status_code == 403
    assert "ai_decisions.approve" in resp.json()["detail"]
    assert decisions.feedback == []


def test_an_unauthenticated_request_is_refused(monkeypatch, decisions):
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    # No token means no query, so the session is never opened.
    app = backend_main.app
    app.dependency_overrides[request_unit_of_work] = lambda: None
    try:
        resp = TestClient(app).post(
            FEEDBACK, json={"human_reviewer": "mallory", "human_decision": "agree"}
        )
    finally:
        app.dependency_overrides.pop(request_unit_of_work, None)

    assert resp.status_code == 401
    assert decisions.feedback == []


def test_the_stats_read_stays_open_to_a_role_without_the_right(viewer, decisions):
    resp = viewer.get("/api/ai/decisions/stats", params={"days": 7})

    assert resp.status_code == 200, resp.text
    assert resp.json()["total_with_feedback"] == 2
    assert resp.json()["period_days"] == 7
