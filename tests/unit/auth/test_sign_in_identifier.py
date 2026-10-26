"""A sign-in string names one account.

Username and email are unique only inside their own column, and login accepts
either. A string that is one account's username and another's email is refused
at every write. A pair of rows that already share one does not authenticate,
and the failure is not charged to either account.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth.auth_service import AuthService
from core.storage.models import Principal, Role, User
from core.storage.models.base import Base
from services.api.routers.auth import _apply_profile_update

pytestmark = pytest.mark.unit

PASSWORD = "correct-horse-battery"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _account(session, user_id, username, email, password_hash):
    session.add(Principal(principal_id=user_id, kind="person", label=username))
    user = User(
        user_id=user_id,
        principal_id=user_id,
        username=username,
        email=email,
        password_hash=password_hash,
        full_name=username,
        role_id="role-viewer",
        is_active=True,
        is_verified=True,
        login_count=0,
        failed_login_count=0,
    )
    session.add(user)
    return user


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[Role.__table__, Principal.__table__, User.__table__]
    )
    session = sessionmaker(bind=engine)()
    session.add(
        Role(
            role_id="role-viewer",
            name="Viewer",
            description="",
            permissions={},
            is_system_role=True,
        )
    )
    session.commit()
    yield session
    session.close()


@pytest.fixture
def password_hash():
    return AuthService.hash_password(PASSWORD)


def test_login_refuses_a_shared_identifier_and_charges_neither_account(
    db, password_hash
):
    victim = _account(
        db,
        "user-victim",
        "b-login@example.test",
        "b@example.test",
        password_hash,
    )
    attacker = _account(
        db, "user-attacker", "attacker", "b-login@example.test", password_hash
    )
    db.commit()
    refusal = {}

    assert (
        AuthService.authenticate_user(
            "b-login@example.test", PASSWORD, db, refusal=refusal
        )
        is None
    )
    assert refusal == {"reason": "ambiguous"}
    assert victim.failed_login_count == 0
    assert attacker.failed_login_count == 0

    refusal.clear()
    assert (
        AuthService.authenticate_user("b@example.test", PASSWORD, db, refusal=refusal)
        is victim
    )
    assert (
        AuthService.authenticate_user("attacker", PASSWORD, db, refusal=refusal)
        is attacker
    )


def test_an_account_may_sign_in_with_a_string_it_uses_for_both_columns(
    db, password_hash
):
    holder = _account(
        db, "user-holder", "same@example.test", "same@example.test", password_hash
    )
    db.commit()

    assert (
        AuthService.authenticate_user("same@example.test", PASSWORD, db) is holder
    )


def test_writes_reject_a_string_another_account_already_uses(db, password_hash):
    victim = _account(
        db,
        "user-victim",
        "b-login@example.test",
        "b@example.test",
        password_hash,
    )
    holder = _account(
        db, "user-holder", "holder", "holder@example.test", password_hash
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        _apply_profile_update(
            db, holder, None, "b-login@example.test", PASSWORD
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Email already in use"
    assert holder.email == "holder@example.test"

    with pytest.raises(HTTPException) as case_exc:
        _apply_profile_update(
            db, holder, None, "B-Login@example.test", PASSWORD
        )
    assert case_exc.value.status_code == 400
    assert holder.email == "holder@example.test"

    assert (
        AuthService.create_user(
            "b-login@example.test",
            "new@example.test",
            PASSWORD,
            "New",
            "role-viewer",
            session=db,
        )
        is None
    )
    assert (
        AuthService.create_user(
            "new-person",
            "B@example.test",
            PASSWORD,
            "New",
            "role-viewer",
            session=db,
        )
        is None
    )
    assert victim.email == "b@example.test"

    changed, _ = _apply_profile_update(
        db, holder, None, holder.email.upper(), PASSWORD
    )
    assert changed is False
    assert holder.email == "holder@example.test"


def test_holder_may_point_their_email_at_their_own_username(db, password_hash):
    holder = _account(
        db,
        "user-holder",
        "holder@example.test",
        "old@example.test",
        password_hash,
    )
    db.commit()

    changed, _ = _apply_profile_update(
        db, holder, None, "holder@example.test", PASSWORD
    )
    assert changed is True
    assert holder.email == "holder@example.test"
    assert (
        AuthService.authenticate_user("holder@example.test", PASSWORD, db) is holder
    )
