"""Case comments, evidence, and escalations append to the trail when it is ready.

A stand-in session is not Postgres, so the legacy ``CaseAuditLog`` insert stays.
These force the trail path. The rollback cases are the ones a comment or an
escalation must not survive.
"""

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from core.audit import trail
from core.cases import case_trail
from core.cases.case_attachment_service import attach_document
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_workflow_service import CaseWorkflowService
from core.response import ceiling
from core.response.approval_service import AlreadyDecided, ApprovalService
from core.storage.models import CaseAuditLog, CaseComment, CaseEvidence

# Direct service calls stand in for a signed-in request, not a tool call.
pytestmark = [pytest.mark.unit, pytest.mark.unbound]


@pytest.fixture(autouse=True)
def _principals(monkeypatch):
    monkeypatch.setattr(
        "core.cases.agent_closure.principal_for_username",
        lambda session, name: {"ada": "prin-ada"}.get(name),
    )


def _trail(monkeypatch):
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    held: dict = {}

    def append(session, payload, **beside):
        held["payload"] = payload
        held["beside"] = beside
        return trail.Entry(1, "hash")

    monkeypatch.setattr(trail, "append", append)
    return held


def _session():
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []

    def flush():
        for call in session.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, CaseComment) and obj.comment_id is None:
                obj.comment_id = 11
            if isinstance(obj, CaseEvidence) and obj.evidence_id is None:
                obj.evidence_id = 5

    session.flush.side_effect = flush
    return session


def test_a_comment_appends_the_principal_and_skips_case_audit_logs(monkeypatch):
    held = _trail(monkeypatch)
    session = _session()

    comment = CaseCollaborationService().add_comment(
        "c-1", "ada", "noted", session=session
    )

    assert comment is not None
    assert comment.comment_id == 11
    assert held["payload"] == {
        "action": "case.comment",
        "actor": "prin-ada",
        "case_id": "c-1",
        "comment_id": 11,
    }
    assert held["beside"] == {"actor_text": "ada"}
    assert not any(
        isinstance(call.args[0], CaseAuditLog) for call in session.add.call_args_list
    )
    session.commit.assert_not_called()


def test_an_unset_caller_is_agent_with_no_principal(monkeypatch):
    held = _trail(monkeypatch)
    session = _session()

    CaseCollaborationService().add_comment("c-1", "agent", "hunt note", session=session)

    assert "actor" not in held["payload"]
    assert held["beside"] == {"actor_text": "agent"}
    session.execute.assert_not_called()


def test_an_owned_comment_trail_failure_returns_none_after_rollback(monkeypatch):
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    monkeypatch.setattr(
        trail, "append", MagicMock(side_effect=OperationalError("insert", {}, None))
    )
    session = _session()
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", lambda: session)

    assert CaseCollaborationService().add_comment("c-1", "ada", "noted") is None
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_a_restore_pause_reaches_the_caller_unwrapped(monkeypatch):
    # The API answers trail.Paused with 503 and its retry time, not a 500.
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    paused = trail.Paused(7, datetime(2026, 10, 27))
    monkeypatch.setattr(trail, "append", MagicMock(side_effect=paused))
    session = _session()
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", lambda: session)

    with pytest.raises(trail.Paused):
        CaseCollaborationService().add_comment("c-1", "ada", "noted")
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_a_joined_comment_trail_failure_propagates(monkeypatch):
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    monkeypatch.setattr(
        trail, "append", MagicMock(side_effect=OperationalError("insert", {}, None))
    )
    session = _session()

    with pytest.raises(case_trail.TrailWriteError):
        CaseCollaborationService().add_comment("c-1", "ada", "noted", session=session)

    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_an_edit_puts_the_previous_text_beside_the_hash(monkeypatch):
    held = _trail(monkeypatch)
    comment = CaseComment(
        comment_id=4,
        case_id="c-1",
        author="bob",
        content="original note",
        is_edited=False,
        is_deleted=False,
    )
    session = _session()
    session.query.return_value.filter.return_value.first.return_value = comment

    assert (
        CaseCollaborationService().update_comment(
            "c-1", 4, "changed", "ada", session=session
        )
        is True
    )
    assert held["payload"] == {
        "action": "case.comment.update",
        "actor": "prin-ada",
        "case_id": "c-1",
        "comment_id": 4,
    }
    assert held["beside"]["operator_text"] == "original note"
    assert "original note" not in held["payload"].values()
    assert not any(
        isinstance(call.args[0], CaseAuditLog) for call in session.add.call_args_list
    )


def test_an_escalation_keeps_the_reason_out_of_the_payload(monkeypatch):
    held = _trail(monkeypatch)
    case = MagicMock()
    case.timeline = []
    session = _session()
    session.query.return_value.filter.return_value.first.return_value = case

    def flush():
        for call in session.add.call_args_list:
            obj = call.args[0]
            if getattr(obj, "escalation_id", None) is None and hasattr(
                obj, "escalated_to"
            ):
                obj.escalation_id = 8

    session.flush.side_effect = flush

    assert (
        CaseWorkflowService().escalate_case(
            "c-1",
            escalated_from="ada",
            escalated_to="soc-lead",
            reason="needs a lead",
            session=session,
        )
        is True
    )
    assert held["payload"]["action"] == "case.escalate"
    assert held["payload"]["actor"] == "prin-ada"
    assert held["payload"]["case_id"] == "c-1"
    assert held["payload"]["escalation_id"] == 8
    assert "needs a lead" not in held["payload"].values()
    assert held["beside"]["operator_text"] == "needs a lead"
    assert not any(
        isinstance(call.args[0], CaseAuditLog) for call in session.add.call_args_list
    )


def test_a_joined_failure_before_the_append_propagates(monkeypatch):
    # The gate and the actor lookup are queries too; a failure there must not
    # look like a comment that was never written.
    monkeypatch.setattr(
        trail, "in_use", MagicMock(side_effect=OperationalError("select", {}, None))
    )
    session = _session()

    with pytest.raises(case_trail.TrailWriteError):
        CaseCollaborationService().add_comment("c-1", "ada", "noted", session=session)

    session.commit.assert_not_called()


def test_a_joined_non_trail_failure_returns_the_default(monkeypatch):
    # Only a trail failure propagates on a joined session. Anything else keeps
    # the old default_on_error contract, which MCP callers read as falsy.
    monkeypatch.setattr(trail, "in_use", lambda session: False)
    session = _session()
    session.query.side_effect = OperationalError("select", {}, None)

    assert (
        CaseCollaborationService().add_comment("c-1", "ada", "noted", session=session)
        is None
    )
    assert (
        CaseWorkflowService().escalate_case(
            "c-1", "ada", "soc-lead", "why", session=session
        )
        is False
    )


def test_a_joined_escalation_trail_failure_propagates(monkeypatch):
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    monkeypatch.setattr(
        trail, "append", MagicMock(side_effect=OperationalError("insert", {}, None))
    )
    case = MagicMock()
    case.timeline = []
    session = _session()
    session.query.return_value.filter.return_value.first.return_value = case

    with pytest.raises(case_trail.TrailWriteError):
        CaseWorkflowService().escalate_case(
            "c-1", "ada", "soc-lead", "needs a lead", session=session
        )

    session.commit.assert_not_called()


def test_an_attachment_names_the_evidence_row(tmp_path, monkeypatch):
    held = _trail(monkeypatch)
    session = _session()
    source = tmp_path / "advisory.pdf"
    source.write_bytes(b"%PDF original")
    store = CaseEvidenceService(storage_path=str(tmp_path / "evidence"))

    row = attach_document(
        session,
        "c-1",
        source,
        "advisory.pdf",
        name=None,
        pages=4,
        collected_by="ada",
        store=store,
    )

    assert row.evidence_id == 5
    assert held["payload"] == {
        "action": "case.evidence",
        "actor": "prin-ada",
        "case_id": "c-1",
        "evidence_id": 5,
    }
    assert not any(
        isinstance(call.args[0], CaseAuditLog) for call in session.add.call_args_list
    )


class _Row:
    def __init__(self):
        self.action_id = "act-1"
        self.action_type = "custom"
        self.title = "t"
        self.description = "d"
        self.target = "x"
        self.confidence = 0.5
        self.reason = "because"
        self.evidence = []
        self.created_at = datetime(2026, 10, 21)
        self.created_by = "agent"
        self.requires_approval = True
        self.status = "pending"
        self.approved_at = None
        self.approved_by = None
        self.approved_by_principal_id = None
        self.executed_at = None
        self.execution_result = None
        self.rejection_reason = None
        self.parameters = {}
        self.workflow_run_id = None
        self.workflow_phase_id = None
        self.reversibility = "reversible"
        self.idempotency_key = None


def _approvals(monkeypatch, session):
    @contextmanager
    def session_scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    manager = MagicMock()
    manager.session_scope = session_scope
    monkeypatch.setattr(
        "core.response.approval_service.get_db_manager", lambda: manager
    )
    monkeypatch.setattr(
        "core.response.ceiling.read_ceiling", lambda: ceiling.Ceiling(four_eyes=False)
    )
    session.get.return_value = _Row()
    return session.get.return_value


def test_approve_appends_the_principal_already_passed_in(monkeypatch):
    held = _trail(monkeypatch)
    session = _session()
    row = _approvals(monkeypatch, session)

    updated = ApprovalService().approve_action(
        "act-1", approved_by="ada", approved_by_principal_id="prin-ada"
    )

    assert updated is not None
    assert updated.status == "approved"
    assert row.approved_by == "ada"
    assert row.approved_by_principal_id == "prin-ada"
    assert held["payload"] == {
        "action": "approval.approve",
        "actor": "prin-ada",
        "action_id": "act-1",
    }
    assert held["beside"] == {"actor_text": "ada"}
    session.commit.assert_called_once()


def test_reject_keeps_the_reason_beside_the_hash(monkeypatch):
    held = _trail(monkeypatch)
    session = _session()
    row = _approvals(monkeypatch, session)

    updated = ApprovalService().reject_action(
        "act-1",
        "not this host",
        rejected_by="ada",
        approved_by_principal_id="prin-ada",
    )

    assert updated.status == "rejected"
    assert row.approved_by == "ada"
    assert row.rejection_reason == "not this host"
    assert held["payload"] == {
        "action": "approval.reject",
        "actor": "prin-ada",
        "action_id": "act-1",
    }
    assert "not this host" not in held["payload"].values()
    assert held["beside"]["operator_text"] == "not this host"


def test_a_failed_approve_leaves_the_transaction_rolled_back(monkeypatch):
    monkeypatch.setattr(trail, "in_use", lambda session: True)
    monkeypatch.setattr(
        trail, "append", MagicMock(side_effect=OperationalError("insert", {}, None))
    )
    session = _session()
    _approvals(monkeypatch, session)

    assert (
        ApprovalService().approve_action(
            "act-1", approved_by="ada", approved_by_principal_id="prin-ada"
        )
        is None
    )
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


def test_a_decision_that_does_not_change_the_row_writes_nothing(monkeypatch):
    held = _trail(monkeypatch)
    session = _session()
    row = _approvals(monkeypatch, session)
    row.status = "approved"

    with pytest.raises(AlreadyDecided):
        ApprovalService().approve_action(
            "act-1", approved_by="ada", approved_by_principal_id="prin-ada"
        )

    assert held == {}
