"""Failed-login bookkeeping must survive the 401 the caller answers with.

The login endpoint hands its request-scoped session to ``authenticate_user`` and
then raises ``HTTPException(401)`` when it returns ``None``. That rolls the
request's transaction back — so if the failed-attempt counter joined that
transaction it would be discarded, the threshold would never trip, and the
brute-force lockout would silently stop working.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import core.storage.unit_of_work as uow_module
from core.auth import auth_service as auth_module
from core.auth.auth_service import AuthService

pytestmark = pytest.mark.unit


def _user(**kwargs):
    user = MagicMock()
    user.user_id = "user-1"
    user.username = "victim"
    user.is_active = True
    user.locked_until = None
    user.failed_login_count = kwargs.get("failed_login_count", 0)
    user.password_hash = "hashed"
    return user


@pytest.fixture
def own_session(monkeypatch):
    """The session ``unit_of_work()`` opens when it is not handed one."""
    session = MagicMock()
    monkeypatch.setattr(uow_module, "get_db_session", lambda: session)
    return session


@pytest.fixture
def caller_session():
    """Stands in for the request-scoped session the endpoint passes down."""
    return MagicMock()


def _wire(session, user):
    session.query.return_value.filter.return_value.first.return_value = user


def test_failed_attempt_is_committed_outside_the_caller_transaction(
    own_session, caller_session, monkeypatch
):
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: False)
    )
    caller_user = _user(failed_login_count=0)
    own_user = _user(failed_login_count=0)
    _wire(caller_session, caller_user)
    _wire(own_session, own_user)

    result = AuthService.authenticate_user("victim", "wrong", caller_session)

    assert result is None
    # The counter must land in its own committed transaction, because the
    # caller is about to roll its own back by raising a 401.
    assert own_user.failed_login_count == 1
    own_session.commit.assert_called_once()
    caller_session.commit.assert_not_called()


def test_threshold_locks_the_account_durably(own_session, caller_session, monkeypatch):
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: False)
    )
    monkeypatch.setattr(auth_module, "LOCKOUT_THRESHOLD", 3)
    caller_user = _user(failed_login_count=2)
    own_user = _user(failed_login_count=2)
    _wire(caller_session, caller_user)
    _wire(own_session, own_user)

    AuthService.authenticate_user("victim", "wrong", caller_session)

    assert own_user.failed_login_count == 3
    assert own_user.locked_until is not None
    assert own_user.locked_until > datetime.utcnow()
    assert own_user.locked_until <= datetime.utcnow() + timedelta(
        minutes=auth_module.LOCKOUT_DURATION_MINUTES
    )
    own_session.commit.assert_called_once()


def test_successful_login_still_joins_the_caller_transaction(
    own_session, caller_session, monkeypatch
):
    """The happy path must NOT open its own transaction — the request commits it."""
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: True)
    )
    user = _user(failed_login_count=2)
    _wire(caller_session, user)

    result = AuthService.authenticate_user("victim", "right", caller_session)

    assert result is user
    assert user.failed_login_count == 0
    assert user.locked_until is None
    caller_session.commit.assert_not_called()
    own_session.commit.assert_not_called()
