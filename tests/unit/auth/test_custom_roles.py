"""Custom roles, the grant check and the recovery command (#2917)."""

import io

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import recover_admin
from core.auth.permissions import CATALOG, PRESETS
from core.storage.models import Principal, Role, User
from core.storage.models.base import Base
from services.api.routers import users

pytestmark = pytest.mark.unit

MEDIC = "medic-a1b2c3d4e5f6"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _user(session, user_id, role_id, *, machine=False, username=None):
    session.add(
        Principal(
            principal_id=user_id,
            kind="service_account" if machine else "person",
            label=user_id,
        )
    )
    user = User(
        user_id=user_id,
        principal_id=user_id,
        username=username or user_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        full_name=user_id,
        role_id=role_id,
        is_active=True,
        service_account=machine,
    )
    session.add(user)
    return user


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[Role.__table__, Principal.__table__, User.__table__]
    )
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", maker)
    session = maker()
    for role_id, grants in CATALOG["builtins"].items():
        session.add(
            Role(role_id=role_id, name=role_id, description="", permissions=grants)
        )
    # Manages roles, but holds nothing to read the trail with.
    session.add(
        Role(
            role_id="role-steward",
            name="Steward",
            description="",
            permissions={
                **CATALOG["builtins"]["role-viewer"],
                "roles.manage": True,
                "users.read": True,
                "users.write": True,
            },
        )
    )
    people = {
        "admin": _user(session, "admin", "role-admin"),
        "steward": _user(session, "steward", "role-steward"),
        "analyst": _user(session, "analyst", "role-analyst"),
        "siem": _user(session, "siem", "role-viewer", machine=True),
        "machine_admin": _user(session, "bot", "role-admin", machine=True),
        "medic": _user(session, "medic", "role-viewer", machine=True, username=MEDIC),
    }
    session.commit()
    yield session, people
    session.close()


def _create(session, actor, **body):
    return users.create_role(users.CreateRoleRequest(**body), actor, session)


def _assign(session, actor, target, role_id):
    return users._apply_user_update(
        session, actor, target.user_id, users.UpdateUserRequest(role_id=role_id)
    )


def test_copying_a_preset_writes_an_ordinary_custom_role(db):
    session, people = db
    role = _create(session, people["admin"], name="Leads", copy_from="soc-lead")
    assert role["kind"] == "custom"
    stored = session.get(Role, role["role_id"])
    assert stored.permissions == PRESETS["soc-lead"]["permissions"]
    assert not stored.permissions.get("platform.write")
    assert not stored.permissions.get("integrations.install")
    # Copied once: the preset is not the row.
    stored.permissions = {**stored.permissions, "cases.read": False}
    assert PRESETS["soc-lead"]["permissions"]["cases.read"] is True


def test_a_refusal_names_the_permission_the_editor_lacks(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        _create(session, people["steward"], name="Audit", copy_from="auditor")
    assert exc.value.status_code == 403
    assert "audit.read" in exc.value.detail
    assert session.query(Role).filter(Role.name == "Audit").first() is None


def test_a_built_in_cannot_be_edited_or_deleted(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        users.update_role(
            "role-viewer",
            users.UpdateRoleRequest(permissions={}),
            people["admin"],
            session,
        )
    assert exc.value.detail == users.BUILTIN_ROLE_REFUSED
    with pytest.raises(HTTPException) as exc:
        users.delete_role("role-viewer", people["admin"], session)
    assert exc.value.detail == users.BUILTIN_ROLE_REFUSED


def test_a_modified_built_in_is_listed_with_its_diff(db):
    session, people = db
    viewer = session.get(Role, "role-viewer")
    viewer.permissions = {**viewer.permissions, "users.read": True}
    session.commit()
    listed = users.list_roles(people["admin"], session)
    entry = next(r for r in listed["roles"] if r["role_id"] == "role-viewer")
    assert entry["modified"] is True
    assert entry["diff"] == {"users.read": {"catalog": False, "role": True}}


def test_a_role_someone_holds_cannot_be_deleted(db):
    session, people = db
    role = _create(session, people["admin"], name="Readers", copy_from="auditor")
    _assign(session, people["admin"], people["analyst"], role["role_id"])
    with pytest.raises(HTTPException) as exc:
        users.delete_role(role["role_id"], people["admin"], session)
    assert exc.value.status_code == 409
    assert exc.value.detail.startswith("1 account still hold")


def test_medic_stays_viewer_on_the_users_api(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        _assign(session, people["admin"], people["medic"], "role-analyst")
    assert exc.value.status_code == 403
    assert "Medic" in exc.value.detail
    assert session.get(User, "medic").role_id == "role-viewer"


def test_another_service_account_may_hold_a_custom_role(db):
    session, people = db
    role = _create(
        session, people["admin"], name="SIEM", copy_from="detection-engineer"
    )
    _assign(session, people["admin"], people["siem"], role["role_id"])
    assert session.get(User, "siem").role_id == role["role_id"]


def test_audit_read_for_a_service_account_needs_audit_forward(db):
    session, people = db
    # Steward holds what the role grants, but not audit.forward.
    session.get(Role, "role-steward").permissions = {
        **session.get(Role, "role-steward").permissions,
        "audit.read": True,
    }
    session.add(
        Role(
            role_id="role-trail",
            name="Trail",
            description="",
            permissions={"audit.read": True},
        )
    )
    session.commit()
    with pytest.raises(HTTPException) as exc:
        _assign(session, people["steward"], people["siem"], "role-trail")
    assert "audit.forward" in exc.value.detail
    _assign(session, people["admin"], people["siem"], "role-trail")
    assert session.get(User, "siem").role_id == "role-trail"


def test_a_service_account_may_narrow_but_not_widen(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        _assign(session, people["machine_admin"], people["analyst"], "role-manager")
    assert "person" in exc.value.detail
    _assign(session, people["machine_admin"], people["analyst"], "role-viewer")
    assert session.get(User, "analyst").role_id == "role-viewer"


def test_a_person_cannot_leave_nobody_able_to_manage_roles(db):
    session, people = db
    session.get(User, "steward").is_active = False
    session.commit()
    with pytest.raises(HTTPException) as exc:
        _assign(session, people["admin"], people["admin"], "role-manager")
    assert exc.value.status_code == 403
    assert "roles.manage" in exc.value.detail
    assert session.get(User, "admin").role_id == "role-admin"


def test_a_machine_narrowing_does_not_trip_the_lockout_guard(db):
    session, people = db
    session.get(User, "steward").is_active = False
    session.commit()
    _assign(session, people["machine_admin"], people["admin"], "role-viewer")
    assert session.get(User, "admin").role_id == "role-viewer"
    assert users.list_roles(people["machine_admin"], session)["nobody_can_manage_roles"]


def test_service_accounts_say_person_only_keys_have_no_effect(db):
    session, people = db
    listed = users._dump_accounts(session, [people["machine_admin"], people["admin"]])
    assert listed[0]["no_effect"] == ["audit.forward", "audit.manage"]
    assert listed[1]["no_effect"] == []


@pytest.fixture
def trail_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(recover_admin.trail, "trail_ready", lambda session: True)
    monkeypatch.setattr(
        recover_admin.trail,
        "append",
        lambda session, payload: calls.append(payload),
    )
    return calls


def test_recovery_makes_a_named_person_admin_with_a_reason(db, trail_calls):
    session, _ = db
    previous = recover_admin.recover("analyst", "only admin left the company\n")
    session.expire_all()
    assert previous == "role-analyst"
    assert session.get(User, "analyst").role_id == "role-admin"
    (entry,) = trail_calls
    assert entry["actor"] == "system"
    assert entry["action"] == "user.role_changed"
    assert entry["reason"] == "only admin left the company"
    assert "roles.manage" in entry["permissions_after"]
    assert "roles.manage" not in entry["permissions_before"]


def test_recovery_needs_a_reason_and_a_person(db, trail_calls, monkeypatch):
    engine = type("Manager", (), {"engine": object()})()
    monkeypatch.setattr(recover_admin, "get_db_manager", lambda: engine)
    err = io.StringIO()
    assert recover_admin.main(["analyst"], io.StringIO("\n"), io.StringIO(), err) == 64
    assert "reason" in err.getvalue()
    assert recover_admin.main(["siem"], io.StringIO("why\n"), io.StringIO(), err) == 77
    assert (
        recover_admin.main(["nobody"], io.StringIO("why\n"), io.StringIO(), err) == 77
    )
    assert trail_calls == []


def test_a_service_account_cannot_re_enable_an_account(db):
    session, people = db
    session.get(User, "analyst").is_active = False
    session.commit()
    with pytest.raises(HTTPException):
        users._apply_user_update(
            session,
            people["machine_admin"],
            "analyst",
            users.UpdateUserRequest(is_active=True),
        )
    assert session.get(User, "analyst").is_active is False
