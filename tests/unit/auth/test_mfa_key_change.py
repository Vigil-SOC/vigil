"""MFA behaviour when JWT_SECRET_KEY no longer matches the one that encrypted the secret."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import bcrypt
import pyotp
import pytest

import core.auth.auth_service as auth_service
from core.auth.auth_service import AuthService

TOTP_SECRET = "JBSWY3DPEHPK3PXP"
RECOVERY_CODE = "ABCD1234"


def _session(user):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user
    return session


def _user(mfa_secret):
    hashed = bcrypt.hashpw(RECOVERY_CODE.encode(), bcrypt.gensalt()).decode()
    return SimpleNamespace(
        username="ada",
        email="ada@example.com",
        mfa_secret=mfa_secret,
        mfa_enabled=True,
        mfa_recovery_codes=[hashed],
    )


@pytest.fixture(autouse=True)
def _reset_log_throttle(monkeypatch):
    monkeypatch.setattr(auth_service, "_last_undecryptable_log", float("-inf"))


@pytest.fixture
def rekeyed_user(monkeypatch):
    """A user whose secret was encrypted under a key that is no longer active."""
    monkeypatch.setattr(auth_service, "JWT_SECRET_KEY", "old-key")
    user = _user(AuthService._encrypt_mfa_secret(TOTP_SECRET))
    monkeypatch.setattr(auth_service, "JWT_SECRET_KEY", "new-key")
    return user


def test_undecryptable_secret_is_reported_not_treated_as_plaintext(
    rekeyed_user, caplog
):
    with caplog.at_level(logging.ERROR, logger=auth_service.logger.name):
        assert (
            AuthService.verify_mfa_code("u", "123456", _session(rekeyed_user)) is False
        )

    assert "cannot be decrypted with the current JWT_SECRET_KEY" in caplog.text
    assert "binascii" not in caplog.text
    assert "verify_mfa_code failed" not in caplog.text
    assert rekeyed_user.mfa_secret not in caplog.text


def test_undecryptable_secret_logs_once_per_interval(rekeyed_user, caplog):
    with caplog.at_level(logging.ERROR, logger=auth_service.logger.name):
        for _ in range(5):
            AuthService.verify_mfa_code("u", "123456", _session(rekeyed_user))

    assert caplog.text.count("cannot be decrypted") == 1


def test_enable_mfa_and_qr_uri_fail_cleanly_on_undecryptable_secret(
    rekeyed_user, caplog
):
    session = _session(rekeyed_user)
    with caplog.at_level(logging.ERROR, logger=auth_service.logger.name):
        assert AuthService.enable_mfa("u", "123456", session) is None
        assert AuthService.get_mfa_qr_uri("u", session) is None

    assert "cannot be decrypted with the current JWT_SECRET_KEY" in caplog.text
    assert "enable_mfa failed" not in caplog.text


def test_recovery_code_still_works_after_key_change_and_is_consumed(rekeyed_user):
    session = _session(rekeyed_user)

    assert AuthService.verify_mfa_code("u", "WRONG000", session) is False
    assert len(rekeyed_user.mfa_recovery_codes) == 1

    assert AuthService.verify_mfa_code("u", RECOVERY_CODE.lower(), session) is True
    assert rekeyed_user.mfa_recovery_codes == []
    assert AuthService.verify_mfa_code("u", RECOVERY_CODE, session) is False


def test_legacy_plaintext_secret_still_verifies():
    user = _user(TOTP_SECRET)
    code = pyotp.TOTP(TOTP_SECRET).now()

    assert AuthService.verify_mfa_code("u", code, _session(user)) is True
    assert AuthService.get_mfa_qr_uri("u", _session(user)).startswith("otpauth://")


def test_encrypted_secret_round_trips_under_the_same_key():
    user = _user(AuthService._encrypt_mfa_secret(TOTP_SECRET))
    code = pyotp.TOTP(TOTP_SECRET).now()

    assert AuthService.verify_mfa_code("u", code, _session(user)) is True
