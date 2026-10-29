"""Case and approval actions on a migrated trail (#2761).

A signed-in comment writes one ``audit_trail`` row and no ``case_audit_logs``
row. A failed append leaves the comment uncommitted, and an approval the same
way: the row stays pending. The audit reader's filter sees the row, and so
do the case record and the case's export, by ``case_id`` (#3308). Edits,
assignments, journal lines, deletes and purges of the case row append the
same way (#3403).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from core.audit import trail
from core.auth.tool_principal import Claims, bound_claims
from core.cases import case_journal_service, case_records_service, case_trail
from core.cases.agent_closure import clear_http_actor, note_http_actor
from core.cases.case_attachment_service import attach_document
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_record import merge_record
from core.cases.case_workflow_service import CaseWorkflowService
from core.response.approval_service import ApprovalService
from core.storage.connection import get_db_manager, get_db_session
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Case, CaseAuditLog
from core.storage.service import DatabaseService
from tests.integration.test_upgrade_command import _run, _scratch

# Direct service calls stand in for a signed-in request, not a tool call.
pytestmark = [pytest.mark.integration, pytest.mark.database, pytest.mark.unbound]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == 0, report
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO principals (principal_id, kind, label) "
                    "VALUES ('prin-ada', 'person', 'Ada')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, principal_id, username, email, password_hash, "
                    "full_name, role_id, is_active, is_verified, mfa_enabled, "
                    "mfa_recovery_codes, login_count) VALUES "
                    "('prin-ada', 'prin-ada', 'ada', 'ada@example.com', 'hash', "
                    "'Ada', 'role-analyst', true, true, false, '[]', 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO cases (case_id, title, description, timeline) "
                    "VALUES ('c-1', 'Hunt', '', '[]'::jsonb)"
                )
            )
        yield engine


@pytest.fixture
def db(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    manager = get_db_manager()
    saved = manager._engine, manager._session_factory
    manager._engine, manager._session_factory = engine, factory
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_trail, audit_trail_held, case_audit_logs, "
                "case_comments, case_escalations, case_evidence, "
                "case_notifications, approval_actions RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        conn.execute(
            text(
                "INSERT INTO cases (case_id, title, description, timeline) "
                "VALUES ('c-1', 'Hunt', '', '[]'::jsonb) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "UPDATE cases SET status = 'open', priority = 'medium', "
                "assignee = NULL, activities = '[]'::jsonb WHERE case_id = 'c-1'"
            )
        )
    try:
        yield engine
    finally:
        manager._engine, manager._session_factory = saved


def _payloads(engine):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT payload, actor_text, operator_text "
                    "FROM audit_trail ORDER BY seq"
                )
            )
        )


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def _approval(engine, action_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, approved_by, approved_by_principal_id, "
                "rejection_reason FROM approval_actions WHERE action_id = :id"
            ),
            {"id": action_id},
        ).one()


def _pending(engine, action_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO approval_actions "
                "(action_id, action_type, title, description, target, reason, "
                "created_by, status) VALUES "
                "(:id, 'custom', 't', 'd', 'host', 'because', 'agent', 'pending')"
            ),
            {"id": action_id},
        )


def _fail(monkeypatch):
    def append(*_args, **_kwargs):
        raise OperationalError("INSERT", {}, Exception("forced"))

    monkeypatch.setattr(trail, "append", append)


def test_a_signed_in_comment_is_one_trail_row_and_no_case_audit_log(db):
    comment = CaseCollaborationService().add_comment("c-1", "ada", "noted")

    assert comment is not None and comment.comment_id
    rows = _payloads(db)
    assert len(rows) == 1
    payload, actor_text, operator_text = rows[0]
    assert payload == {
        "action": "case.comment",
        "actor": "prin-ada",
        "case_id": "c-1",
        "comment_id": comment.comment_id,
    }
    assert actor_text == "ada"
    assert operator_text is None
    assert _count(db, "case_audit_logs") == 0
    assert _count(db, "case_comments") == 1

    factory = sessionmaker(bind=db)
    with factory() as session:
        shown = trail.read(session, trail.make_query(area="case"))
        assert [row["payload"]["action"] for row in shown] == ["case.comment"]
        assert trail.verify(session).ok
        assert case_records_service.list_case_audit(session, "c-1") == []


def test_an_unset_caller_records_agent_and_no_principal(db):
    CaseCollaborationService().add_comment("c-1", "agent", "from the hunt")

    payload, actor_text, _operator = _payloads(db)[0]
    assert payload["action"] == "case.comment"
    assert "actor" not in payload
    assert actor_text == "agent"
    assert _count(db, "case_audit_logs") == 0


def test_a_failing_append_leaves_no_comment(db, monkeypatch):
    _fail(monkeypatch)

    assert CaseCollaborationService().add_comment("c-1", "ada", "noted") is None

    assert _count(db, "case_comments") == 0
    assert _count(db, "audit_trail") == 0
    assert _count(db, "case_audit_logs") == 0


def test_a_joined_trail_failure_is_not_committed(db, monkeypatch):
    _fail(monkeypatch)
    session = get_db_session()
    try:
        with pytest.raises(case_trail.TrailWriteError):
            CaseCollaborationService().add_comment(
                "c-1", "ada", "noted", session=session
            )
        session.rollback()
    finally:
        session.close()

    assert _count(db, "case_comments") == 0
    assert _count(db, "audit_trail") == 0


def test_an_escalation_and_an_attachment_skip_case_audit_logs(db, tmp_path):
    assert (
        CaseWorkflowService().escalate_case("c-1", "ada", "soc-lead", "needs a lead")
        is True
    )
    source = tmp_path / "advisory.pdf"
    source.write_bytes(b"%PDF original")
    with sessionmaker(bind=db)() as session:
        evidence = attach_document(
            session,
            "c-1",
            source,
            "advisory.pdf",
            name=None,
            pages=2,
            collected_by="ada",
            store=CaseEvidenceService(storage_path=str(tmp_path / "evidence")),
        )
        session.commit()
        evidence_id = evidence.evidence_id

    rows = _payloads(db)
    assert [row[0]["action"] for row in rows] == ["case.escalate", "case.evidence"]
    escalation, attachment = rows
    assert escalation[0]["actor"] == "prin-ada"
    assert escalation[0]["case_id"] == "c-1"
    assert isinstance(escalation[0]["escalation_id"], int)
    assert "needs a lead" not in escalation[0].values()
    assert escalation[2] == "needs a lead"
    assert attachment[0]["evidence_id"] == evidence_id
    assert attachment[1] == "ada"
    assert _count(db, "case_audit_logs") == 0


def test_approve_appends_and_a_failed_append_stays_pending(db, monkeypatch):
    _pending(db, "act-ok")
    approved = ApprovalService().approve_action(
        "act-ok", approved_by="ada", approved_by_principal_id="prin-ada"
    )

    assert approved is not None and approved.status == "approved"
    assert _approval(db, "act-ok") == ("approved", "ada", "prin-ada", None)
    payload, actor_text, operator_text = _payloads(db)[0]
    assert payload == {
        "action": "approval.approve",
        "actor": "prin-ada",
        "action_id": "act-ok",
    }
    assert (actor_text, operator_text) == ("ada", None)

    _pending(db, "act-no")
    _fail(monkeypatch)
    assert (
        ApprovalService().approve_action(
            "act-no", approved_by="ada", approved_by_principal_id="prin-ada"
        )
        is None
    )
    assert _approval(db, "act-no")[0] == "pending"
    assert [row[0]["action_id"] for row in _payloads(db)] == ["act-ok"]


def test_without_the_trail_table_a_comment_still_commits(db):
    """create_all plus a head stamp has the revision and no audit_trail."""
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE audit_trail RENAME TO audit_trail_hidden"))
    try:
        comment = CaseCollaborationService().add_comment("c-1", "ada", "legacy")
        assert comment is not None
        assert (
            CaseWorkflowService().escalate_case("c-1", "ada", "soc-lead", "because")
            is True
        )
        with db.connect() as conn:
            audit = conn.execute(
                text(
                    "SELECT action, changed_by FROM case_audit_logs "
                    "WHERE entity_id = 'c-1'"
                )
            ).one()
        assert audit == ("update", "ada")
        assert _count(db, "case_comments") == 1
    finally:
        with db.begin() as conn:
            conn.execute(text("ALTER TABLE audit_trail_hidden RENAME TO audit_trail"))


def test_reject_records_the_reason_beside_the_hash(db):
    _pending(db, "act-no")
    rejected = ApprovalService().reject_action(
        "act-no",
        "not this host",
        rejected_by="ada",
        approved_by_principal_id="prin-ada",
    )

    assert rejected is not None and rejected.status == "rejected"
    status, approved_by, principal, reason = _approval(db, "act-no")
    assert (status, approved_by, principal, reason) == (
        "rejected",
        "ada",
        "prin-ada",
        "not this host",
    )
    payload, actor_text, operator_text = _payloads(db)[0]
    assert payload == {
        "action": "approval.reject",
        "actor": "prin-ada",
        "action_id": "act-no",
    }
    assert "not this host" not in payload.values()
    assert (actor_text, operator_text) == ("ada", "not this host")


def test_case_actions_are_on_its_record_and_its_export_with_the_label(db, tmp_path):
    service = CaseCollaborationService()
    service.add_comment("c-1", "ada", "noted")
    CaseWorkflowService().escalate_case("c-1", "ada", "soc-lead", "needs a lead")
    source = tmp_path / "a.pdf"
    source.write_bytes(b"%PDF original")
    with sessionmaker(bind=db)() as session:
        attach_document(
            session,
            "c-1",
            source,
            "a.pdf",
            name=None,
            pages=1,
            collected_by="ada",
            store=CaseEvidenceService(storage_path=str(tmp_path / "evidence")),
        )
        session.commit()

    with sessionmaker(bind=db)() as session, session.begin():
        entries = case_records_service.list_case_trail(session, "c-1")
        texts = [row["text"] for row in merge_record([], [], trail=entries)]
        assert texts == [
            "Ada attached a file to the case",
            "Ada escalated the case",
            "Ada commented on the case",
        ]
        rows, _tip = trail.export(session, trail.make_query(object_ref="c-1"), "system")
        assert [r["payload"]["action"] for r in rows] == [
            "case.comment",
            "case.escalate",
            "case.evidence",
        ]
        assert rows[0]["actor"]["label"] == "Ada"


def test_a_legacy_row_shows_once_and_a_dropped_table_reads_none(db):
    with sessionmaker(bind=db)() as session, session.begin():
        session.add(
            CaseAuditLog(
                entity_type="case",
                entity_id="c-1",
                action="update",
                change_summary="old",
                changed_by="ada",
            )
        )
    with sessionmaker(bind=db)() as session, session.begin():
        audits = case_records_service.list_case_audit(session, "c-1")
        assert [r["id"] for r in merge_record([], audits)] == [
            f"audit:{audits[0].audit_id}"
        ]
        trail.append(
            session,
            {
                "action": "case.comment",
                "case_id": "c-1",
                "attribution": "unverified_legacy",
                "legacy": {"table": "case_audit_logs", "id": audits[0].audit_id},
            },
        )
        entries = case_records_service.list_case_trail(session, "c-1")
        ids = [r["id"] for r in merge_record([], audits, trail=entries)]
        assert ids == [f"trail:{entries[0]['seq']}"]

    with db.begin() as conn:
        conn.execute(text("ALTER TABLE case_audit_logs RENAME TO case_audit_logs_gone"))
    try:
        with sessionmaker(bind=db)() as session:
            assert case_records_service.list_case_audit(session, "c-1") == []
    finally:
        with db.begin() as conn:
            conn.execute(
                text("ALTER TABLE case_audit_logs_gone RENAME TO case_audit_logs")
            )


def test_entries_name_each_principal_and_mark_deleted_and_erased(db):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO principals (principal_id, kind, label, deleted_at) VALUES "
                "('prin-gone', 'person', 'Grace', now()), "
                "('prin-erased', 'person', NULL, NULL) "
                "ON CONFLICT (principal_id) DO NOTHING"
            )
        )
    with sessionmaker(bind=db)() as session, session.begin():
        for actor in ("prin-ada", "agent:hunt", "system", "prin-gone", "prin-erased"):
            trail.append(
                session,
                {
                    "action": "case.comment",
                    "actor": actor,
                    "case_id": "c-2",
                    "on_behalf_of": "prin-ada",
                },
            )
        found = [
            (
                e["actor"]["label"],
                e["actor"]["kind"],
                e["actor"]["deleted"],
                e["actor"]["erased"],
                e["on_behalf_of"]["label"],
                e["sentence"],
            )
            for e in trail.read(session, trail.make_query(object_ref="c-2"))
        ]
    said = "Commented on the case"
    assert found == [
        ("Ada", "person", False, False, "Ada", said),
        ("Hunt", "agent", False, False, "Ada", said),
        ("the system", "system", False, False, "Ada", said),
        ("Grace", "person", True, False, "Ada", said),
        (None, "person", False, True, "Ada", said),
    ]


@pytest.fixture
def ada():
    """Signed in as ada, as get_current_user notes her for the request."""
    note_http_actor(username="ada", principal_id="prin-ada", via="cookie", person=True)
    try:
        yield
    finally:
        clear_http_actor()


def _data(monkeypatch) -> DatabaseDataService:
    monkeypatch.setattr(
        "core.storage.database_data_service.is_demo_mode", lambda: False
    )
    data = DatabaseDataService()
    data._db_service, data._db_connected = DatabaseService(), True
    return data


def _case(engine, column):
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT {column} FROM cases WHERE case_id = 'c-1'")
        ).scalar_one()


def _verified(engine):
    with sessionmaker(bind=engine)() as session:
        return trail.verify(session).ok


@pytest.mark.usefixtures("ada")
def test_a_status_change_is_one_entry_and_a_repeat_is_none(db):
    service = DatabaseService()
    assert service.update_case("c-1", status="investigating") is True
    assert service.update_case("c-1", status="investigating") is True

    rows = _payloads(db)
    assert len(rows) == 1
    payload, actor_text, operator_text = rows[0]
    assert payload == {
        "action": "case.status",
        "actor": "prin-ada",
        "case_id": "c-1",
        "changed": ["status"],
        "status": "investigating",
    }
    assert (actor_text, operator_text) == ("ada", None)
    assert _verified(db)
    with sessionmaker(bind=db)() as session:
        (entry,) = trail.read(session, trail.make_query(object_ref="c-1"))
        assert entry["sentence"] == "Changed status to Investigating"


@pytest.mark.usefixtures("ada")
def test_an_assignment_names_the_principal_and_keeps_the_name_beside(db):
    assert DatabaseService().update_case("c-1", assignee="ada") is True
    assert DatabaseService().update_case("c-1", assignee=None) is True

    (assigned, *_), (unassigned, *_) = _payloads(db)
    assert assigned["action"] == "case.assign"
    assert assigned["assignee"] == "prin-ada"
    assert "ada" not in assigned.values()
    assert _payloads(db)[0][2] == "ada"
    assert unassigned["unassigned"] is True
    assert _verified(db)


@pytest.mark.usefixtures("ada")
def test_a_journal_append_is_one_entry_and_one_record_row(db, monkeypatch):
    entry = case_journal_service.append_activity(
        _data(monkeypatch), "c-1", "note", "Isolated the host"
    )

    assert entry is not None
    (payload, actor_text, operator_text), *more = _payloads(db)
    assert more == []
    assert payload["action"] == "case.journal"
    assert payload["activity"] == 0
    assert "Isolated the host" not in payload.values()
    assert operator_text == "Isolated the host"
    with sessionmaker(bind=db)() as session:
        activities = session.get(Case, "c-1").activities
        entries = case_records_service.list_case_trail(session, "c-1")
        rows = merge_record([], [], activities, trail=entries)
    assert [(r["kind"], r["text"]) for r in rows] == [
        ("human", "Ada added to the journal — Isolated the host")
    ]
    assert _verified(db)


def test_an_agent_tool_update_is_the_agents_on_behalf_of_the_person(db):
    from core.integrations.mcp.surface import acting_as

    claims = Claims(
        username="ada",
        person_id="prin-ada",
        agent_id="agent:hunt",
        run_id=None,
        origin="person",
    )
    with acting_as("ada"), bound_claims(claims):
        assert DatabaseService().update_case("c-1", priority="high") is True

    (payload, actor_text, _), *more = _payloads(db)
    assert more == []
    assert payload == {
        "action": "case.priority",
        "actor": "agent:hunt",
        "case_id": "c-1",
        "on_behalf_of": "prin-ada",
        "changed": ["priority"],
        "priority": "high",
    }
    assert actor_text == "agent:hunt"
    assert _verified(db)


@pytest.mark.usefixtures("ada")
def test_a_refused_append_leaves_the_case_unchanged(db, monkeypatch):
    _fail(monkeypatch)

    with pytest.raises(case_trail.TrailWriteError):
        DatabaseService().update_case("c-1", status="investigating")
    with pytest.raises(case_trail.TrailWriteError):
        case_journal_service.append_activity(_data(monkeypatch), "c-1", "note", "lost")

    assert _case(db, "status") == "open"
    assert _case(db, "activities") == []
    assert _count(db, "audit_trail") == 0


@pytest.mark.usefixtures("ada")
def test_a_close_is_the_close_entry_alone(db):
    assert DatabaseService().update_case("c-1", status="closed") is True
    assert _payloads(db) == []


@pytest.mark.usefixtures("ada")
def test_a_delete_and_a_purge_are_one_entry_each(db):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (case_id, title, description, timeline) VALUES "
                "('c-2', 'Phish', '', '[]'::jsonb), ('c-3', 'Beacon', '', '[]'::jsonb)"
            )
        )
    with sessionmaker(bind=db)() as session, session.begin():
        assert case_records_service.delete_case(session, "c-2") == []
    with sessionmaker(bind=db)() as session, session.begin():
        assert case_records_service.purge_all_cases(session).cases == 2

    deleted, purged = _payloads(db)
    assert deleted[0] == {
        "action": "case.delete",
        "actor": "prin-ada",
        "case_id": "c-2",
    }
    assert deleted[2] == "Phish"
    assert purged[0] == {
        "action": "case.purge",
        "actor": "prin-ada",
        "count": 2,
        "killed_investigations": 0,
    }
    assert _count(db, "config_audit_log") == 0
    assert _verified(db)


def test_a_paused_trail_answers_503_and_leaves_the_case(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    from core.api.v1 import cases_router
    from services.api import main as backend_main
    from services.api.middleware import auth as auth_module

    monkeypatch.setattr(cases_router, "data_service", _data(monkeypatch))
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    until = datetime.now(timezone.utc) + timedelta(seconds=30)

    def paused(*_args, **_kwargs):
        raise trail.Paused(1, until)

    monkeypatch.setattr(trail, "append", paused)
    app = backend_main.app
    with sessionmaker(bind=db)() as session:
        user = session.query(auth_module.User).filter_by(username="ada").one()
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    try:
        response = TestClient(app).patch(
            "/api/v1/cases/c-1", json={"status": "investigating"}
        )
    finally:
        app.dependency_overrides.pop(auth_module.get_current_user, None)

    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) >= 1
    assert _case(db, "status") == "open"


@pytest.mark.usefixtures("ada")
def test_a_close_from_the_route_is_the_close_entry_alone(db, monkeypatch):
    from fastapi.testclient import TestClient

    from core.api.v1 import cases_router
    from services.api import main as backend_main
    from services.api.middleware import auth as auth_module

    monkeypatch.setattr(cases_router, "data_service", _data(monkeypatch))
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    app = backend_main.app
    with sessionmaker(bind=db)() as session:
        user = session.query(auth_module.User).filter_by(username="ada").one()
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    try:
        response = TestClient(app).patch("/api/v1/cases/c-1", json={"status": "closed"})
    finally:
        app.dependency_overrides.pop(auth_module.get_current_user, None)

    assert response.status_code == 200
    assert _case(db, "status") == "closed"
    (payload, _, _), *more = _payloads(db)
    assert more == []
    assert (payload["action"], payload["case_id"]) == ("case.close", "c-1")
    assert _verified(db)


@pytest.mark.usefixtures("ada")
def test_a_refused_close_from_the_route_leaves_the_case_open(db, monkeypatch):
    from fastapi.testclient import TestClient

    from core.api.v1 import cases_router
    from services.api import main as backend_main
    from services.api.middleware import auth as auth_module

    monkeypatch.setattr(cases_router, "data_service", _data(monkeypatch))
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    _fail(monkeypatch)
    app = backend_main.app
    with sessionmaker(bind=db)() as session:
        user = session.query(auth_module.User).filter_by(username="ada").one()
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    try:
        response = TestClient(app, raise_server_exceptions=False).patch(
            "/api/v1/cases/c-1", json={"status": "closed"}
        )
    finally:
        app.dependency_overrides.pop(auth_module.get_current_user, None)

    assert response.status_code == 500
    assert _case(db, "status") == "open"
    assert _count(db, "audit_trail") == 0


@pytest.mark.usefixtures("ada")
def test_a_refused_close_from_an_agent_tool_leaves_the_case_open(db, monkeypatch):
    from core.agents import tool_registry

    monkeypatch.setattr(tool_registry, "permission_refusal", lambda *_a: None)
    monkeypatch.setattr(tool_registry, "_data", lambda: _data(monkeypatch))
    _fail(monkeypatch)

    with pytest.raises(OperationalError):
        tool_registry.update_case(case_id="c-1", status="closed")
    assert _case(db, "status") == "open"
    assert _count(db, "audit_trail") == 0


@pytest.mark.usefixtures("ada")
def test_a_findings_link_is_one_entry_from_either_path(db, monkeypatch):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO findings "
                "(finding_id, data_source, created_at, updated_at, timestamp) "
                "VALUES ('f-1', 'splunk', now(), now(), now()), "
                "('f-2', 'splunk', now(), now(), now()) ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(text("DELETE FROM case_findings WHERE case_id = 'c-1'"))

    # The route's path (the journal service) and the daemon's (add_finding_to_case).
    assert case_journal_service.link_finding(_data(monkeypatch), "c-1", "f-1") is True
    assert DatabaseService().add_finding_to_case("c-1", "f-2") is True

    rows = [row[0] for row in _payloads(db)]
    assert [(p["action"], p["linked"]) for p in rows] == [
        ("case.findings.link", ["f-1"]),
        ("case.findings.link", ["f-2"]),
    ]
    assert _verified(db)


@pytest.mark.usefixtures("ada")
def test_a_reopen_is_a_status_entry(db):
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'closed' WHERE case_id = 'c-1'"))

    assert DatabaseService().update_case("c-1", status="open") is True

    (payload, *_), *more = _payloads(db)
    assert more == []
    assert (payload["action"], payload["status"]) == ("case.status", "open")


@pytest.mark.usefixtures("ada")
def test_without_the_trail_a_case_write_appends_nothing(db, monkeypatch):
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE audit_trail RENAME TO audit_trail_hidden"))
    try:
        assert DatabaseService().update_case("c-1", status="investigating") is True
        assert case_journal_service.append_activity(
            _data(monkeypatch), "c-1", "note", "kept"
        )
        assert _case(db, "status") == "investigating"
        assert _count(db, "case_audit_logs") == 0
    finally:
        with db.begin() as conn:
            conn.execute(text("ALTER TABLE audit_trail_hidden RENAME TO audit_trail"))
    assert _count(db, "audit_trail") == 0
