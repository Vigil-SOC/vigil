"""PUT /api/users/{id} and DELETE must not touch an account the caller does not outrank.

users.write (and users.delete) are not a blanket grant. The caller's
permissions have to cover the target account's current role, the same way
assigning a role already requires covering that role.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.storage.models import Role, User
from core.storage.models.base import Base
from services.api.routers import users

pytestmark = pytest.mark.unit

ROLES_SQL = (
    Path(__file__).parents[3] / "infra" / "database" / "init" / "06_auth_tables.sql"
)


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _seeded_roles() -> dict[str, dict]:
    rows = re.findall(
        r"\('(role-[\w-]+)', '[^']+', '[^']*', '(\{.*?\})', true\)",
        ROLES_SQL.read_text(),
        re.DOTALL,
    )
    return {role_id: json.loads(grants) for role_id, grants in rows}


SEEDED = _seeded_roles()


def _role(session, role_id: str, permissions: dict) -> None:
    session.add(
        Role(
            role_id=role_id,
            name=role_id,
            description="",
            permissions=permissions,
            is_system_role=True,
        )
    )


def _user(session, user_id: str, role_id: str, **kw) -> User:
    user = User(
        user_id=user_id,
        username=kw.get("username", user_id),
        email=kw.get("email", f"{user_id}@example.com"),
        password_hash="x",
        full_name=kw.get("full_name", user_id),
        role_id=role_id,
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    return user


@pytest.fixture
def db(monkeypatch):
    # check_permission opens its own session through the unit of work, so that
    # session has to be this database rather than the process-wide one.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[Role.__table__, User.__table__])
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", maker)
    session = maker()
    for role_id in ("role-admin", "role-manager", "role-viewer"):
        _role(session, role_id, SEEDED[role_id])
    # users.delete without the rest of admin, so delete is not a superset grant.
    _role(
        session,
        "role-ops",
        {"users.read": True, "users.write": True, "users.delete": True},
    )
    _role(session, "role-clerk", {"users.read": True})
    admin = _user(
        session, "admin", "role-admin", full_name="Admin", email="admin@example.com"
    )
    other_admin = _user(
        session,
        "admin-2",
        "role-admin",
        email="admin-2@example.com",
        full_name="Other Admin",
    )
    manager = _user(session, "mgr", "role-manager")
    other_manager = _user(
        session, "mgr-2", "role-manager", email="mgr-2@example.com", full_name="Other"
    )
    viewer = _user(session, "view", "role-viewer", full_name="Viewer")
    ops = _user(session, "ops", "role-ops")
    clerk = _user(session, "clerk", "role-clerk", email="clerk@example.com")
    session.commit()
    yield session, {
        "admin": admin,
        "other_admin": other_admin,
        "manager": manager,
        "other_manager": other_manager,
        "viewer": viewer,
        "ops": ops,
        "clerk": clerk,
    }
    session.close()


def _reload(session, user_id: str) -> User:
    session.expire_all()
    return session.get(User, user_id)


@pytest.mark.parametrize(
    "payload",
    [
        {"role_id": "role-viewer"},
        {"is_active": False},
        {"email": "other@example.com"},
        {"full_name": "Taken Over"},
    ],
)
def test_manager_cannot_change_an_admin(db, payload):
    session, people = db
    admin = people["admin"]
    before = (admin.role_id, admin.is_active, admin.email, admin.full_name)

    with pytest.raises(HTTPException) as exc:
        users._apply_user_update(
            session,
            people["manager"],
            admin.user_id,
            users.UpdateUserRequest(**payload),
        )

    assert exc.value.status_code == 403
    assert "privileges you do not have" in exc.value.detail
    assert (admin.role_id, admin.is_active, admin.email, admin.full_name) == before


def test_manager_can_update_a_lesser_account(db):
    session, people = db
    updated, reason, _ = users._apply_user_update(
        session,
        people["manager"],
        people["viewer"].user_id,
        users.UpdateUserRequest(full_name="Renamed", is_active=False),
    )
    assert updated.full_name == "Renamed"
    assert updated.is_active is False
    assert reason is None


def test_manager_cannot_promote_a_lesser_account_to_admin(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        users._apply_user_update(
            session,
            people["manager"],
            people["viewer"].user_id,
            users.UpdateUserRequest(role_id="role-admin"),
        )
    assert exc.value.status_code == 403
    assert "more privileges" in exc.value.detail
    assert _reload(session, "view").role_id == "role-viewer"


def test_admin_can_demote_another_admin(db):
    session, people = db
    updated, reason, _ = users._apply_user_update(
        session,
        people["admin"],
        people["other_admin"].user_id,
        users.UpdateUserRequest(role_id="role-viewer"),
    )
    assert updated.role_id == "role-viewer"
    assert reason == "Role changed"


def test_same_privileges_can_edit_each_other(db):
    session, people = db
    updated, _, _ = users._apply_user_update(
        session,
        people["manager"],
        people["other_manager"].user_id,
        users.UpdateUserRequest(full_name="Still a manager"),
    )
    assert updated.full_name == "Still a manager"
    assert updated.role_id == "role-manager"


def test_delete_of_a_stronger_role_is_refused(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        users.delete_user(people["admin"].user_id, people["ops"], session)
    assert exc.value.status_code == 403
    assert "privileges you do not have" in exc.value.detail
    assert _reload(session, "admin") is not None

    # A lesser account whose grants ops already holds can be deleted.
    # A viewer cannot: that role can read findings, which ops cannot.
    users.delete_user(people["clerk"].user_id, people["ops"], session)
    session.flush()
    assert session.get(User, "clerk") is None


def test_delete_still_requires_users_delete(db):
    session, people = db
    with pytest.raises(HTTPException) as exc:
        users.delete_user(people["viewer"].user_id, people["manager"], session)
    assert exc.value.status_code == 403
    assert "users.delete" in exc.value.detail
    assert _reload(session, "view") is not None
