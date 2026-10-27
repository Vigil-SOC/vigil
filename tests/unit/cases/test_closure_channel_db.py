"""close_case writes the channel and the principal, from the session (#3192)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.cases.agent_closure import (
    bound_credential,
    clear_http_actor,
    note_http_actor,
    record_close,
)
from core.cases.case_journal_service import append_activity
from core.cases.case_state import closure_view
from core.cases.case_workflow_service import CaseWorkflowService
from core.cases.closure import ClosureCategory
from core.integrations.mcp.surface import acting_as
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseClosureInfo, Principal, Role, User

pytestmark = [pytest.mark.unit, pytest.mark.external_service]


@pytest.fixture
def session():
    db = get_db_session()
    try:
        yield db
        db.rollback()
    finally:
        clear_http_actor()
        db.close()


def _user(session, username: str) -> User:
    role = session.query(Role).first()
    if role is None:
        role = Role(
            role_id=f"r-{uuid.uuid4().hex[:8]}",
            name=f"analyst-{uuid.uuid4().hex[:6]}",
            description="",
            permissions={},
        )
        session.add(role)
        session.flush()
    user_id = f"u-{uuid.uuid4().hex[:8]}"
    session.add(Principal(principal_id=user_id, kind="person", label=username))
    user = User(
        user_id=user_id,
        principal_id=user_id,
        username=username,
        email=f"{username}@example.com",
        password_hash="x",
        full_name=username,
        role_id=role.role_id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _case(session, case_id: str) -> None:
    session.add(
        Case(
            case_id=case_id,
            title="beaconing",
            description="",
            status="open",
            priority="high",
        )
    )
    session.flush()


def test_a_cookie_close_is_analyst_and_stores_the_principal(session):
    user = _user(session, "nestor")
    _case(session, "c-cookie")
    note_http_actor(
        username=user.username,
        principal_id=user.principal_id,
        via="cookie",
        person=True,
    )

    closure = CaseWorkflowService().close_case(
        session,
        "c-cookie",
        closure_category=ClosureCategory.FALSE_POSITIVE,
        closed_by=user.username,
    )

    assert closure.close_channel == "cookie"
    assert closure.closed_by == user.principal_id
    assert closure.closed_by_text == "nestor"
    view = closure_view(closure)
    assert view["closed_by_kind"] == "analyst"
    assert view["closed_by"] == "nestor"


def test_a_bearer_close_is_agent_and_keeps_the_person(session):
    user = _user(session, "ada")
    _case(session, "c-bearer")
    note_http_actor(
        username=user.username,
        principal_id=user.principal_id,
        via="bearer",
        person=True,
    )

    closure = CaseWorkflowService().close_case(
        session,
        "c-bearer",
        closure_category=ClosureCategory.RESOLVED,
        closed_by=user.username,
    )

    assert closure.close_channel == "agent"
    assert closure.closed_by == user.principal_id
    assert closure.closed_by_text == "ada"
    assert closure_view(closure)["closed_by_kind"] == "agent"
    assert closure_view(closure)["closed_by"] == "ada"


def test_a_credential_close_is_agent_and_the_trail_names_the_credential(
    session, monkeypatch
):
    user = _user(session, "mina")
    _case(session, "c-cred")
    captured = {}
    monkeypatch.setattr(
        "core.cases.case_workflow_service.record_close",
        lambda *a, **k: captured.update(k),
    )
    with acting_as(user.username), bound_credential("mcpc-1"):
        closure = CaseWorkflowService().close_case(
            session,
            "c-cred",
            closure_category=ClosureCategory.RESOLVED,
            closed_by=user.username,
        )

    assert closure.close_channel == "agent"
    assert closure.closed_by == user.principal_id
    assert closure.closed_by_text == "mina"
    assert captured["principal_id"] == user.principal_id
    assert captured["actor_text"] == "mina"
    assert captured["credential_id"] == "mcpc-1"


def test_a_run_id_stays_text_and_an_agent_reclose_drops_trust(session):
    _case(session, "c-run")
    workflow = CaseWorkflowService()
    note_http_actor(
        username="nestor", principal_id="u-nestor", via="cookie", person=True
    )
    # No such user: the name is not a live username, so it stays text.
    first = workflow.close_case(
        session,
        "c-run",
        closure_category=ClosureCategory.FALSE_POSITIVE,
        closed_by="not-a-user",
    )
    assert first.close_channel == "cookie"
    assert first.closed_by == "not-a-user"
    assert first.closed_by_text == "not-a-user"

    clear_http_actor()
    again = workflow.close_case(
        session,
        "c-run",
        closure_category=ClosureCategory.RESOLVED,
        closed_by="run-1",
    )
    assert again.close_channel == "agent"
    assert again.closed_by == "run-1"
    assert again.closed_by_text == "run-1"
    assert closure_view(again)["closed_by_kind"] == "agent"


def test_an_unstated_close_does_not_overwrite_an_analyst_determination(session):
    _case(session, "c-keep")
    workflow = CaseWorkflowService()
    note_http_actor(
        username="nestor", principal_id="u-x", via="cookie", person=True
    )
    workflow.close_case(
        session,
        "c-keep",
        closure_category=ClosureCategory.FALSE_POSITIVE,
        closed_by="nestor",
        root_cause="the scanner",
    )
    clear_http_actor()
    workflow.close_case(
        session,
        "c-keep",
        closure_category=ClosureCategory.UNSPECIFIED,
        closed_by="agent",
    )
    recorded = session.get(CaseClosureInfo, "c-keep")
    assert recorded.close_channel == "cookie"
    assert recorded.closed_by_text == "nestor"
    assert recorded.root_cause == "the scanner"
    assert closure_view(recorded)["closed_by_kind"] == "analyst"


def test_a_new_activity_from_the_session_stores_the_principal(session):
    user = _user(session, "ada-act")
    _case(session, "c-act")
    session.commit()
    note_http_actor(
        username=user.username,
        principal_id=user.principal_id,
        via="cookie",
        person=True,
    )
    # The journal opens its own session, so the user has to be committed.
    saved = {}

    class _Data:
        def get_case(self, case_id):
            return {"case_id": case_id, "activities": []}

        def update_case(self, case_id, **updates):
            saved.update(updates)
            return True

    try:
        entry = append_activity(_Data(), "c-act", "note", "looked")
        assert entry["actor"] == user.principal_id
        assert entry["actor_text"] == "ada-act"
        assert saved["activities"][0]["actor"] == user.principal_id
    finally:
        session.query(Case).filter_by(case_id="c-act").delete()
        session.query(User).filter_by(user_id=user.user_id).delete()
        session.query(Principal).filter_by(principal_id=user.principal_id).delete()
        session.commit()


def test_record_close_appends_when_the_trail_table_is_there(session, monkeypatch):
    """The three readiness checks, exercised against this database.

    create_all stamps the head and does not build audit_trail, so the append
    is skipped. A database that has the table takes the row in this transaction.
    """
    present = (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is not None
    )
    if not present:
        called = {}
        monkeypatch.setattr(
            "core.cases.agent_closure.trail.append",
            lambda *a, **k: called.setdefault("n", 0),
        )
        record_close(
            session,
            principal_id="u-1",
            case_id="c-1",
            actor_text="nestor",
            credential_id="mcpc-1",
        )
        assert called == {}
        return
    record_close(
        session,
        principal_id="u-1",
        case_id="c-1",
        actor_text="nestor",
    )
    row = session.execute(
        text(
            "SELECT payload->>'actor', actor_text FROM public.audit_trail "
            "WHERE payload->>'case_id' = 'c-1'"
        )
    ).one()
    assert row == ("u-1", "nestor")
