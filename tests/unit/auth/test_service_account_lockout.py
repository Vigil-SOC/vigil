"""A service account is never locked out; every other account still is.

Medic's gateway logs in as a Viewer service account (D2-17). If anyone could lock
that account with a few bad passwords, they could blind Medic. The exemption is
safe only because the password is long, random and held by the gateway alone;
failed attempts are still counted and logged, so brute force stays visible.
"""

import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

import core.storage.unit_of_work as uow_module
from core.auth import auth_service as auth_module
from core.auth.auth_service import AccountLockedError, AuthService
from core.time import utcnow

pytestmark = pytest.mark.unit


def _user(service_account: bool):
    user = MagicMock()
    user.user_id = "user-1"
    user.username = "medic-abcdefghijkl" if service_account else "viewer"
    user.is_active = True
    user.locked_until = None
    user.failed_login_count = 0
    user.password_hash = "hashed"
    user.service_account = service_account
    return user


@pytest.fixture
def wrong_password(monkeypatch):
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: False)
    )


def _attempt(monkeypatch, user, times: int) -> None:
    """``times`` failed logins against one shared user row."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user
    monkeypatch.setattr(uow_module, "get_db_session", lambda: session)
    for _ in range(times):
        assert AuthService.authenticate_user(user.username, "wrong", session) is None


def test_ten_bad_passwords_lock_a_normal_viewer(monkeypatch, wrong_password):
    user = _user(service_account=False)

    with pytest.raises(AccountLockedError):
        _attempt(monkeypatch, user, 10)

    assert user.locked_until is not None
    assert user.locked_until > utcnow()


def test_fifty_bad_passwords_never_lock_a_service_account(monkeypatch, wrong_password):
    user = _user(service_account=True)

    _attempt(monkeypatch, user, 50)

    assert user.locked_until is None
    # Still counted: the attempts are evidence, not noise.
    assert user.failed_login_count == 50


def test_failed_service_account_logins_log_at_warning(
    monkeypatch, wrong_password, caplog
):
    user = _user(service_account=True)

    with caplog.at_level(logging.WARNING, logger=auth_module.logger.name):
        _attempt(monkeypatch, user, auth_module.LOCKOUT_THRESHOLD)

    exempt = [r for r in caplog.records if "service account" in r.getMessage().lower()]
    assert len(exempt) == 1, [r.getMessage() for r in caplog.records]
    assert exempt[0].levelno == logging.WARNING
    # Fixed template, %-style: the username is an argument, never the format.
    assert "%" in exempt[0].msg
    assert user.username not in exempt[0].msg


def test_a_lock_already_on_a_service_account_does_not_refuse_it(monkeypatch):
    """A lock written before the flag was set (or by hand) can't blind Medic."""
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: True)
    )
    user = _user(service_account=True)
    user.locked_until = utcnow() + timedelta(minutes=10)
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user

    assert AuthService.authenticate_user(user.username, "right", session) is user
    assert user.locked_until is None


def test_a_lock_on_a_normal_account_still_refuses_the_right_password(monkeypatch):
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *a, **k: True)
    )
    user = _user(service_account=False)
    user.locked_until = utcnow() + timedelta(minutes=10)
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user

    with pytest.raises(AccountLockedError):
        AuthService.authenticate_user(user.username, "right", session)


@pytest.mark.parametrize("flag", [None, 0, "", "true"])
def test_only_a_true_flag_exempts(monkeypatch, wrong_password, flag):
    """A missing or truthy-but-not-True value must not exempt (fails closed)."""
    user = _user(service_account=False)
    user.service_account = flag

    with pytest.raises(AccountLockedError):
        _attempt(monkeypatch, user, 10)
