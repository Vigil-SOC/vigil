"""Case and approval actions on a migrated trail (#2761).

A signed-in comment writes one ``audit_trail`` row and no ``case_audit_logs``
row; so do evidence, an IOC, a bulk IOC add and a watcher (#3427). A failed append leaves the comment uncommitted, and an approval the same
way: the row stays pending. The audit reader's filter sees the row, and so
do the case record and the case's export, by ``case_id`` (#3308).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from core.audit import trail
from core.cases import case_records_service, case_trail
from core.cases.case_attachment_service import attach_document
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_record import merge_record
from core.cases.case_workflow_service import CaseWorkflowService
from core.response.approval_service import ApprovalService
from core.storage.connection import get_db_manager, get_db_session
from core.storage.models import CaseAuditLog, User
from core.time import utcnow
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]


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
                "case_notifications, case_iocs, case_watchers, approval_actions "
                "RESTART IDENTITY CASCADE"
            )
        )
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
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
def ada_client(db, monkeypatch):
    """The mounted app, signed in as ada."""
    from services.api import main as backend_main
    from services.api.middleware import auth as auth_module

    user = User(
        user_id="prin-ada",
        principal_id="prin-ada",
        username="ada",
        email="ada@example.com",
        password_hash="",
        role_id="role-analyst",
        is_active=True,
        mfa_enabled=False,
    )
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    app = backend_main.app
    overrides = (auth_module.get_current_user, auth_module.get_current_active_user)
    for dep in overrides:
        app.dependency_overrides[dep] = lambda: user
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


def _verified(engine):
    with sessionmaker(bind=engine)() as session:
        return trail.verify(session).ok


def test_evidence_and_custody_through_rest_are_one_entry_each(db, ada_client):
    resp = ada_client.post(
        "/api/v1/cases/c-1/evidence",
        json={"evidence_type": "log", "name": "ada-laptop auth.log"},
    )
    assert resp.status_code == 200, resp.text
    evidence_id = resp.json()["evidence_id"]
    resp = ada_client.post(
        f"/api/cases/c-1/evidence/{evidence_id}/chain-of-custody",
        json={"action": "transferred", "notes": "handed to Grace"},
    )
    assert resp.status_code == 200, resp.text

    evidence, custody = _payloads(db)
    assert evidence[0] == {
        "action": "case.evidence",
        "actor": "prin-ada",
        "case_id": "c-1",
        "evidence_id": evidence_id,
    }
    assert evidence[1:] == ("ada", "ada-laptop auth.log")
    assert custody[0] == {
        "action": "case.evidence.custody",
        "actor": "prin-ada",
        "case_id": "c-1",
        "evidence_id": evidence_id,
        "custody_action": "transferred",
    }
    assert custody[2] == "handed to Grace"
    with db.connect() as conn:
        chain = conn.execute(
            text("SELECT chain_of_custody FROM case_evidence")
        ).scalar_one()
    assert [step["action"] for step in chain] == ["collected", "transferred"]
    assert _verified(db)

    with sessionmaker(bind=db)() as session, session.begin():
        entries = case_records_service.list_case_trail(session, "c-1")
        audits = case_records_service.list_case_audit(session, "c-1")
        texts = [r["text"] for r in merge_record([], audits, trail=entries)]
    assert texts == [
        "Ada added to an evidence item's chain of custody: transferred",
        "Ada added evidence to the case",
    ]


def test_a_hunt_attachment_is_one_evidence_entry_with_its_hashes(db, tmp_path):
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
        held = (evidence.evidence_id, evidence.file_hash_sha256, evidence.file_hash_md5)
        session.commit()

    [(payload, actor_text, operator_text)] = _payloads(db)
    assert payload["action"] == "case.evidence"
    assert (
        payload["evidence_id"],
        payload["file_hash_sha256"],
        payload["file_hash_md5"],
    ) == held
    assert held[1]
    assert "advisory.pdf" not in payload.values()
    assert (actor_text, operator_text) == ("ada", "advisory.pdf")
    assert _count(db, "case_audit_logs") == 0
    assert _verified(db)


def test_an_ioc_is_one_entry_and_its_value_sits_beside_the_hash(db, ada_client):
    body = {"ioc_type": "domain", "value": "evil.example"}
    resp = ada_client.post("/api/v1/cases/c-1/iocs", json=body)
    assert resp.status_code == 200, resp.text
    # The same IOC again adds no row and no entry.
    assert ada_client.post("/api/v1/cases/c-1/iocs", json=body).status_code == 200

    [(payload, actor_text, operator_text)] = _payloads(db)
    assert payload == {
        "action": "case.ioc",
        "actor": "prin-ada",
        "case_id": "c-1",
        "ioc_id": resp.json()["ioc_id"],
        "ioc_type": "domain",
    }
    assert (actor_text, operator_text) == ("ada", "evil.example")
    assert _verified(db)


def test_a_bulk_ioc_add_is_one_entry_with_the_count(db, ada_client):
    resp = ada_client.post(
        "/api/v1/cases/c-1/iocs/bulk",
        json={
            "iocs": [
                {"ioc_type": "ip", "value": "10.0.0.5"},
                {"ioc_type": "ip", "value": "10.0.0.6"},
                {"ioc_type": "domain", "value": "evil.example"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"added": 3}

    [(payload, _actor, operator_text)] = _payloads(db)
    assert payload["action"] == "case.ioc.bulk"
    assert payload["count"] == 3
    assert payload["ioc_types"] == ["domain", "ip"]
    assert len(payload["ioc_ids"]) == 3
    assert "10.0.0.5" in operator_text
    assert _count(db, "case_iocs") == 3
    assert _verified(db)


def test_adding_and_removing_a_watcher_name_the_watcher_by_principal(db, ada_client):
    resp = ada_client.post("/api/cases/c-1/watchers", json={"user_id": "ada"})
    assert resp.status_code == 200, resp.text
    resp = ada_client.delete("/api/cases/c-1/watchers/ada")
    assert resp.status_code == 200, resp.text

    added, removed = _payloads(db)
    for entry, action in (
        (added, "case.watcher.add"),
        (removed, "case.watcher.remove"),
    ):
        assert entry[0] == {
            "action": action,
            "actor": "prin-ada",
            "case_id": "c-1",
            "watcher": "prin-ada",
        }
        assert entry[2] is None
    assert _count(db, "case_watchers") == 0
    assert _verified(db)


@pytest.mark.parametrize(
    "method, path, body, table",
    [
        (
            "post",
            "/api/v1/cases/c-1/evidence",
            {"evidence_type": "log", "name": "x"},
            "case_evidence",
        ),
        (
            "post",
            "/api/v1/cases/c-1/iocs",
            {"ioc_type": "ip", "value": "10.0.0.5"},
            "case_iocs",
        ),
        (
            "post",
            "/api/v1/cases/c-1/iocs/bulk",
            {"iocs": [{"ioc_type": "ip", "value": "1"}]},
            "case_iocs",
        ),
        ("post", "/api/cases/c-1/watchers", {"user_id": "ada"}, "case_watchers"),
    ],
)
def test_a_refused_append_rolls_back_the_row_and_a_pause_is_503(
    db, ada_client, monkeypatch, method, path, body, table
):
    _fail(monkeypatch)
    resp = getattr(ada_client, method)(path, json=body)
    assert resp.status_code == 500, resp.text
    assert _count(db, table) == 0
    assert _count(db, "audit_trail") == 0

    def paused(*_args, **_kwargs):
        raise trail.Paused(1, utcnow() + timedelta(seconds=30))

    monkeypatch.setattr(trail, "append", paused)
    resp = getattr(ada_client, method)(path, json=body)
    assert resp.status_code == 503, resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    assert _count(db, table) == 0


def test_removing_a_watcher_rolls_back_when_the_append_is_refused(
    db, ada_client, monkeypatch
):
    assert (
        ada_client.post("/api/cases/c-1/watchers", json={"user_id": "ada"}).status_code
        == 200
    )
    _fail(monkeypatch)
    assert ada_client.delete("/api/cases/c-1/watchers/ada").status_code == 500
    assert _count(db, "case_watchers") == 1


def test_a_missing_case_is_still_a_404(db, ada_client):
    for path, body in (
        ("/api/v1/cases/nope/evidence", {"evidence_type": "log", "name": "x"}),
        ("/api/v1/cases/nope/iocs", {"ioc_type": "ip", "value": "10.0.0.5"}),
        ("/api/cases/nope/watchers", {"user_id": "ada"}),
    ):
        assert ada_client.post(path, json=body).status_code == 404, path
    assert _count(db, "audit_trail") == 0
