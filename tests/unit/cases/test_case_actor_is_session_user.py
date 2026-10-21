"""Case comments, evidence, custody, links and escalations name the signed-in user.

Those routes used to store the actor the request body carried, so any
``cases.write`` holder could write as someone else, and the console stored
every row as ``SOC Analyst``. These run against the mounted app so the
router's auth dependency is the real one; the services are stubs. The
comment edit and escalation tests below the router checks use the real
services against a stand-in session.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core.cases import case_records_service
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_workflow_service import CaseWorkflowService
from core.routing import request_unit_of_work
from core.storage.models import (
    CaseAuditLog,
    CaseComment,
    CaseEscalation,
    CaseEvidence,
    CaseRelationship,
    User,
)
from services.api import main as backend_main
from services.api.middleware import auth as auth_module

pytestmark = pytest.mark.unit

WHEN = datetime(2026, 10, 21)


@pytest.fixture()
def alice(monkeypatch):
    """Signed in as alice, holding cases.write. Records what each service stored."""
    user = User(
        user_id="alice",
        username="alice",
        email="alice@test.local",
        password_hash="",
        role_id="role-analyst",
        is_active=True,
        mfa_enabled=False,
    )
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    recorded: dict = {}

    def add_comment(
        self, case_id, author, content, parent_comment_id=None, session=None
    ):
        recorded["comment"] = author
        return CaseComment(
            comment_id=1,
            case_id=case_id,
            author=author,
            content=content,
            parent_comment_id=parent_comment_id,
            mentions=[],
            attachment_ids=[],
            is_edited=False,
            is_deleted=False,
            created_at=WHEN,
            updated_at=WHEN,
        )

    def add_evidence(self, **kwargs):
        recorded["evidence"] = kwargs["collected_by"]
        return CaseEvidence(
            evidence_id=7,
            case_id=kwargs["case_id"],
            evidence_type=kwargs["evidence_type"],
            name=kwargs["name"],
            description=kwargs.get("description"),
            collected_by=kwargs["collected_by"],
            collected_at=WHEN,
            chain_of_custody=[],
            tags=kwargs.get("tags") or [],
            created_at=WHEN,
            updated_at=WHEN,
        )

    def add_custody(self, evidence_id, action, user, notes=None, session=None):
        recorded["custody"] = user
        return True

    def update_comment(self, case_id, comment_id, new_content, edited_by, session=None):
        recorded["edit"] = (case_id, comment_id, edited_by)
        return True

    def delete_comment(
        self, case_id, comment_id, deleted_by, soft_delete=True, session=None
    ):
        recorded["delete"] = (case_id, comment_id, deleted_by)
        return True

    def escalate(
        self,
        case_id,
        escalated_from,
        escalated_to,
        reason,
        urgency_level="high",
        session=None,
    ):
        recorded["escalation"] = escalated_from
        return True

    def add_relationship(
        session,
        case_id,
        *,
        related_case_id,
        relationship_type,
        created_by,
        notes=None,
    ):
        recorded["relationship"] = created_by
        return CaseRelationship(
            relationship_id=3,
            case_id=case_id,
            related_case_id=related_case_id,
            relationship_type=relationship_type,
            created_by=created_by,
            notes=notes,
            created_at=WHEN,
        )

    monkeypatch.setattr(CaseCollaborationService, "add_comment", add_comment)
    monkeypatch.setattr(CaseCollaborationService, "update_comment", update_comment)
    monkeypatch.setattr(CaseCollaborationService, "delete_comment", delete_comment)
    monkeypatch.setattr(CaseEvidenceService, "add_evidence", add_evidence)
    monkeypatch.setattr(CaseEvidenceService, "add_chain_of_custody_entry", add_custody)
    monkeypatch.setattr(CaseWorkflowService, "escalate_case", escalate)
    monkeypatch.setattr(case_records_service, "add_relationship", add_relationship)

    app = backend_main.app
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    app.dependency_overrides[auth_module.get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: None
    try:
        yield TestClient(app), recorded
    finally:
        app.dependency_overrides.pop(auth_module.get_current_user, None)
        app.dependency_overrides.pop(auth_module.get_current_active_user, None)
        app.dependency_overrides.pop(request_unit_of_work, None)


def test_a_comment_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/comments",
        json={"author": "bob", "content": "Closing as benign"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["comment"] == "alice"
    assert resp.json()["author"] == "alice"


def test_a_comment_without_an_author_is_accepted(alice):
    client, recorded = alice
    resp = client.post("/api/cases/c-1/comments", json={"content": "noted"})

    assert resp.status_code == 200, resp.text
    assert recorded["comment"] == "alice"


def test_evidence_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/v1/cases/c-1/evidence",
        json={"evidence_type": "file", "name": "pcap", "collected_by": "bob"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["evidence"] == "alice"
    assert resp.json()["collected_by"] == "alice"


def test_evidence_without_collected_by_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence",
        json={"evidence_type": "log", "name": "auth.log"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["evidence"] == "alice"


def test_a_custody_entry_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence/7/chain-of-custody",
        json={"action": "transferred", "user": "bob", "notes": "to the lab"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["custody"] == "alice"


def test_a_custody_entry_without_a_user_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence/7/chain-of-custody",
        json={"action": "analyzed"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["custody"] == "alice"


def test_a_relationship_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/relationships",
        json={
            "related_case_id": "c-2",
            "relationship_type": "related",
            "created_by": "bob",
        },
    )

    assert resp.status_code == 200, resp.text
    assert recorded["relationship"] == "alice"
    assert resp.json()["created_by"] == "alice"


def test_a_relationship_without_created_by_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/relationships",
        json={"related_case_id": "c-2", "relationship_type": "duplicate"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["relationship"] == "alice"


def test_an_escalation_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/escalate",
        json={
            "escalated_from": "bob",
            "escalated_to": "soc-lead",
            "reason": "needs a lead",
        },
    )

    assert resp.status_code == 200, resp.text
    assert recorded["escalation"] == "alice"


def test_an_escalation_without_escalated_from_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/escalate",
        json={"escalated_to": "soc-lead", "reason": "needs a lead"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["escalation"] == "alice"


def test_an_edit_names_alice_and_stays_on_the_case(alice):
    client, recorded = alice
    resp = client.put(
        "/api/cases/c-1/comments/4",
        json={"content": "changed"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["edit"] == ("c-1", 4, "alice")


def test_a_delete_names_alice_and_stays_on_the_case(alice):
    client, recorded = alice
    resp = client.delete("/api/cases/c-1/comments/4")

    assert resp.status_code == 200, resp.text
    assert recorded["delete"] == ("c-1", 4, "alice")


def test_editing_a_missing_comment_is_not_found(alice, monkeypatch):
    client, _recorded = alice

    def missing(self, case_id, comment_id, new_content, edited_by, session=None):
        return False

    monkeypatch.setattr(CaseCollaborationService, "update_comment", missing)
    resp = client.put("/api/cases/c-1/comments/4", json={"content": "changed"})

    assert resp.status_code == 404


def _comment(content: str = "original note") -> CaseComment:
    return CaseComment(
        comment_id=4,
        case_id="c-1",
        author="bob",
        content=content,
        is_edited=False,
        is_deleted=False,
    )


def _session_holding(comment):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = comment
    return session


def _filter_text(session) -> str:
    clauses = session.query.return_value.filter.call_args.args
    return " ".join(str(clause) for clause in clauses)


def test_an_edit_keeps_the_previous_text_under_the_editor():
    comment = _comment()
    session = _session_holding(comment)

    updated = CaseCollaborationService().update_comment(
        "c-1", 4, "changed", "alice", session=session
    )

    assert updated is True
    assert comment.content == "changed"
    assert comment.is_edited is True
    audit = session.add.call_args.args[0]
    assert isinstance(audit, CaseAuditLog)
    assert audit.entity_type == "case"
    assert audit.entity_id == "c-1"
    assert audit.changed_by == "alice"
    assert audit.old_value == "original note"
    assert audit.new_value == "changed"
    assert audit.change_summary == (
        "alice edited comment 4. Previous text: original note"
    )
    assert "case_id" in _filter_text(session)
    assert "comment_id" in _filter_text(session)


def test_a_delete_keeps_the_previous_text_under_who_deleted_it():
    comment = _comment()
    session = _session_holding(comment)

    deleted = CaseCollaborationService().delete_comment(
        "c-1", 4, "alice", session=session
    )

    assert deleted is True
    assert comment.is_deleted is True
    assert comment.content == "[deleted]"
    audit = session.add.call_args.args[0]
    assert audit.action == "delete"
    assert audit.changed_by == "alice"
    assert audit.old_value == "original note"
    assert "Previous text: original note" in audit.change_summary


def test_an_edit_of_a_comment_that_is_not_on_the_case_writes_nothing():
    session = _session_holding(None)

    updated = CaseCollaborationService().update_comment(
        "c-1", 4, "changed", "alice", session=session
    )

    assert updated is False
    session.add.assert_not_called()


def test_an_escalation_records_the_actor_it_was_given_on_the_case():
    case = MagicMock()
    case.timeline = []
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = case

    escalated = CaseWorkflowService().escalate_case(
        "c-1",
        escalated_from="alice",
        escalated_to="soc-lead",
        reason="needs a lead",
        session=session,
    )

    assert escalated is True
    added = [call.args[0] for call in session.add.call_args_list]
    escalation = next(row for row in added if isinstance(row, CaseEscalation))
    audit = next(row for row in added if isinstance(row, CaseAuditLog))
    assert escalation.escalated_from == "alice"
    assert escalation.escalated_to == "soc-lead"
    assert audit.entity_type == "case"
    assert audit.entity_id == "c-1"
    assert audit.changed_by == "alice"
    assert audit.change_summary == (
        "alice escalated this case to soc-lead: needs a lead"
    )
    assert case.timeline == [
        {
            "timestamp": case.timeline[0]["timestamp"],
            "event": "alice escalated to soc-lead: needs a lead",
        }
    ]
