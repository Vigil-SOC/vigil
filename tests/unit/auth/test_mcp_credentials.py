"""A credential a program holds is not a session, and opens only what it names.

These run against SQLite rather than a live Postgres: what is under test is the
service's rules -- what it stores, what it refuses, and where each credential
is accepted -- none of which is dialect-specific.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from core.auth import mcp_credential_service as svc
from core.storage.models import McpCredential, Role, User
from core.storage.models.base import Base
from core.time import utcnow


# `users` and `roles` carry JSONB columns this has no interest in, and SQLite
# cannot render the type at all. Registered for the SQLite dialect only, so
# nothing that speaks to Postgres is affected.
@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[Role.__table__, User.__table__, McpCredential.__table__]
    )
    maker = sessionmaker(bind=engine)
    s = maker()
    s.add(Role(role_id="r-analyst", name="analyst", description="", permissions={}))
    s.add(
        User(
            user_id="u-1",
            username="nestor",
            email="nestor@example.com",
            password_hash="x",
            full_name="Nestor",
            role_id="r-analyst",
            is_active=True,
        )
    )
    s.commit()
    yield s
    s.close()


def test_the_token_is_returned_once_and_never_stored(session):
    minted = svc.mint("u-1", "the platform", session=session)

    assert minted is not None
    stored = session.query(McpCredential).one()
    assert minted.token not in (stored.token_hash, stored.label)
    assert stored.token_hash == hashlib.sha256(minted.token.encode()).hexdigest()


def test_a_token_says_what_it_is(session):
    minted = svc.mint("u-1", "the platform", session=session)

    assert minted.token.startswith(svc.TOKEN_PREFIX)
    assert svc.looks_like_mcp_token(minted.token)


def test_a_credential_stands_for_its_user(session):
    minted = svc.mint("u-1", "the platform", session=session)

    user = svc.authenticate(minted.token, session=session)

    assert user is not None
    assert user.user_id == "u-1"


def test_a_credential_carries_no_permissions_of_its_own(session):
    """What the holder may do is what the user may do, through one role model."""
    minted = svc.mint("u-1", "the platform", session=session)

    user = svc.authenticate(minted.token, session=session)

    assert user.role_id == "r-analyst"
    assert not hasattr(minted.record, "permissions")


def test_a_session_token_is_not_one_of_these(session):
    a_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.signature"

    assert svc.looks_like_mcp_token(a_jwt) is False
    assert svc.authenticate(a_jwt, session=session) is None


def test_a_revoked_credential_stops_working(session):
    minted = svc.mint("u-1", "the platform", session=session)

    assert svc.revoke(minted.record.credential_id, session=session) is True
    assert svc.authenticate(minted.token, session=session) is None


def test_revoking_twice_reports_that_it_did_nothing(session):
    minted = svc.mint("u-1", "the platform", session=session)
    svc.revoke(minted.record.credential_id, session=session)

    assert svc.revoke(minted.record.credential_id, session=session) is False


def test_an_expired_credential_stops_working(session):
    minted = svc.mint(
        "u-1",
        "yesterday's",
        expires_at=utcnow() - timedelta(minutes=1),
        session=session,
    )

    assert svc.authenticate(minted.token, session=session) is None


def test_a_credential_without_an_expiry_keeps_working(session):
    minted = svc.mint("u-1", "standing", session=session)

    assert minted.record.expires_at is None
    assert svc.authenticate(minted.token, session=session) is not None


def test_a_deactivated_user_takes_its_credentials_with_it(session):
    minted = svc.mint("u-1", "the platform", session=session)
    session.query(User).filter(User.user_id == "u-1").one().is_active = False
    session.flush()

    assert svc.authenticate(minted.token, session=session) is None


def test_revoking_one_credential_leaves_the_others(session):
    first = svc.mint("u-1", "laptop", session=session)
    second = svc.mint("u-1", "the platform", session=session)

    svc.revoke(first.record.credential_id, session=session)

    assert svc.authenticate(first.token, session=session) is None
    assert svc.authenticate(second.token, session=session) is not None


def test_rotation_works_because_both_live_at_once(session):
    """Mint the new one, move the caller across, then withdraw the old one."""
    old = svc.mint("u-1", "the platform", session=session)
    new = svc.mint("u-1", "the platform, rotated", session=session)

    assert svc.authenticate(old.token, session=session) is not None
    assert svc.authenticate(new.token, session=session) is not None

    svc.revoke(old.record.credential_id, session=session)

    assert svc.authenticate(old.token, session=session) is None
    assert svc.authenticate(new.token, session=session) is not None


def test_revoking_a_credential_does_not_touch_the_account(session):
    minted = svc.mint("u-1", "the platform", session=session)
    before = session.query(User).filter(User.user_id == "u-1").one().password_hash

    svc.revoke(minted.record.credential_id, session=session)

    user = session.query(User).filter(User.user_id == "u-1").one()
    assert user.password_hash == before
    assert user.is_active is True


def test_a_credential_is_not_minted_for_someone_who_does_not_exist(session):
    assert svc.mint("u-nobody", "nowhere", session=session) is None
    assert session.query(McpCredential).count() == 0


def test_an_unknown_token_authenticates_nobody(session):
    assert (
        svc.authenticate(svc.TOKEN_PREFIX + "not-one-we-issued", session=session)
        is None
    )


def test_use_is_recorded_so_an_idle_credential_can_be_found(session):
    minted = svc.mint("u-1", "the platform", session=session)
    assert minted.record.last_used_at is None

    svc.authenticate(minted.token, session=session)

    assert session.query(McpCredential).one().last_used_at is not None


def test_listing_hides_revoked_ones_unless_asked(session):
    kept = svc.mint("u-1", "kept", session=session)
    gone = svc.mint("u-1", "gone", session=session)
    svc.revoke(gone.record.credential_id, session=session)

    live = svc.list_for_user("u-1", session=session)
    everything = svc.list_for_user("u-1", include_revoked=True, session=session)

    assert [c.credential_id for c in live] == [kept.record.credential_id]
    assert len(everything) == 2


def test_what_an_operator_sees_never_includes_the_token(session):
    minted = svc.mint("u-1", "the platform", session=session)

    shown = minted.record.to_dict()

    assert minted.token not in str(shown)
    assert "token_hash" not in shown
