"""Self-service MFA routes: setup is refused while enabled (409), and turning
MFA off needs a current TOTP or recovery code and clears the stored codes.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import core.auth.auth_service as auth_service
import core.storage.unit_of_work as uow_module
from core.routing import request_unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit


@pytest.fixture
def user():
    return SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        mfa_enabled=False,
        mfa_secret=None,
        mfa_recovery_codes=[],
        mfa_last_totp_step=None,
        failed_login_count=0,
        locked_until=None,
    )


@pytest.fixture
def client(user, monkeypatch):
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.populate_existing.return_value.with_for_update.return_value = query
    query.first.return_value = user
    # Wrong MFA codes are counted in a separate transaction. Same user row.
    counter = MagicMock()
    counter.query.return_value.filter.return_value.first.return_value = user
    monkeypatch.setattr(uow_module, "get_db_session", lambda: counter)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: session
    limiter.reset()
    client = TestClient(app)
    client.session = session
    client.counter = counter
    return client


def _code(secret, t):
    return pyotp.TOTP(secret).at(int(t))


def _enrol(client, user):
    """Setup + verify through the routes; returns (secret, recovery_codes)."""
    secret = client.post("/api/auth/mfa/setup").json()["secret"]
    resp = client.post(
        "/api/auth/mfa/verify", json={"code": _code(secret, auth_service.time.time())}
    )
    assert resp.status_code == 200
    return secret, resp.json()["recovery_codes"]


def test_setup_then_verify_enables_mfa_and_returns_codes(client, user):
    setup = client.post("/api/auth/mfa/setup")
    assert setup.status_code == 200
    assert setup.json()["qr_uri"].startswith("otpauth://totp/")
    assert user.mfa_enabled is False  # not enabled until the first code is proven

    _, codes = _enrol(client, user)
    assert user.mfa_enabled is True
    assert len(codes) == 10 and len(user.mfa_recovery_codes) == 10


def test_setup_while_enabled_is_409_and_changes_nothing(client, user):
    _enrol(client, user)
    secret, hashes = user.mfa_secret, list(user.mfa_recovery_codes)

    resp = client.post("/api/auth/mfa/setup")

    assert resp.status_code == 409
    assert user.mfa_enabled is True
    assert user.mfa_secret == secret
    assert user.mfa_recovery_codes == hashes


def test_disable_with_wrong_code_is_refused_and_leaves_mfa_on(client, user):
    _enrol(client, user)

    resp = client.request("DELETE", "/api/auth/mfa", json={"code": "000000"})

    assert resp.status_code == 400
    assert user.mfa_enabled is True
    assert user.mfa_secret is not None
    assert len(user.mfa_recovery_codes) == 10


def test_disable_with_totp_clears_secret_and_recovery_codes(clock, client, user):
    secret, _ = _enrol(client, user)
    clock.now = T0 + 35  # the enrolment step is spent

    resp = client.request(
        "DELETE", "/api/auth/mfa", json={"code": _code(secret, clock.now)}
    )

    assert resp.status_code == 200
    assert user.mfa_enabled is False
    assert user.mfa_secret is None
    assert user.mfa_recovery_codes == []


def test_disable_with_recovery_code_clears_the_rest(client, user):
    _, codes = _enrol(client, user)

    resp = client.request("DELETE", "/api/auth/mfa", json={"code": codes[0]})

    assert resp.status_code == 200
    assert user.mfa_enabled is False
    assert user.mfa_recovery_codes == []


def test_disable_when_mfa_is_off_is_400(client, user):
    resp = client.request("DELETE", "/api/auth/mfa", json={"code": "123456"})
    assert resp.status_code == 400


def test_disable_requires_a_code_in_the_body(client, user):
    _enrol(client, user)
    assert client.request("DELETE", "/api/auth/mfa").status_code == 422
    assert user.mfa_enabled is True


def test_disable_is_rate_limited(client, user, monkeypatch):
    # Lockout is a separate control; hold it back so this asserts the limiter.
    monkeypatch.setattr(auth_service, "LOCKOUT_THRESHOLD", 100)
    _enrol(client, user)
    statuses = [
        client.request("DELETE", "/api/auth/mfa", json={"code": "000000"}).status_code
        for _ in range(6)
    ]
    assert statuses == [400] * 5 + [429]


# --- TOTP replay (RFC 6238 §5.2): a code's step is accepted once ----------

T0 = 1_800_000_000  # aligned to a 30 s step boundary


@pytest.fixture
def clock(monkeypatch):
    """Freeze the clock the verifier reads; move it with clock.now = ..."""
    clock = SimpleNamespace(now=T0 + 5)
    monkeypatch.setattr(auth_service.time, "time", lambda: clock.now)
    return clock


@pytest.fixture
def login(monkeypatch, user):
    """POST /api/auth/login for `user`, password check and tokens stubbed."""
    monkeypatch.setattr(
        auth.AuthService, "authenticate_user", staticmethod(lambda *a: user)
    )
    monkeypatch.setattr(
        auth.AuthService, "generate_jwt_token", staticmethod(lambda *a, **k: "tok")
    )
    monkeypatch.setattr(
        auth.AuthService, "verify_jwt_token", staticmethod(lambda t: {})
    )
    monkeypatch.setattr(auth, "_user_payload", lambda u, s: {})

    def _login(client, code):
        return client.post(
            "/api/auth/login",
            json={"username_or_email": "analyst", "password": "pw", "mfa_code": code},
        )

    return _login


def test_login_code_is_refused_the_second_time_within_its_step(
    clock, client, user, login
):
    secret, _ = _enrol(client, user)  # enrols on step T0
    clock.now = T0 + 35
    code = _code(secret, clock.now)

    assert login(client, code).status_code == 200
    clock.now = T0 + 50  # same step
    assert login(client, code).status_code == 401
    # The next step's code still works.
    clock.now = T0 + 65
    assert login(client, _code(secret, clock.now)).status_code == 200


def test_code_from_an_earlier_step_is_refused(clock, client, user, login):
    secret, _ = _enrol(client, user)
    clock.now = T0 + 65
    assert login(client, _code(secret, clock.now)).status_code == 200

    # Still inside the ±1 window, but older than the accepted step.
    assert login(client, _code(secret, T0 + 35)).status_code == 401


def test_enrolment_code_cannot_disable_mfa(clock, client, user):
    secret, _ = _enrol(client, user)
    enrol_code = _code(secret, clock.now)

    resp = client.request("DELETE", "/api/auth/mfa", json={"code": enrol_code})
    assert resp.status_code == 400
    assert user.mfa_enabled is True

    clock.now = T0 + 35
    resp = client.request(
        "DELETE", "/api/auth/mfa", json={"code": _code(secret, clock.now)}
    )
    assert resp.status_code == 200
    assert user.mfa_enabled is False


def test_setup_resets_the_stored_step(clock, client, user):
    _enrol(client, user)
    user.mfa_enabled = False  # as after a disable

    secret = client.post("/api/auth/mfa/setup").json()["secret"]
    assert user.mfa_last_totp_step is None
    resp = client.post("/api/auth/mfa/verify", json={"code": _code(secret, clock.now)})
    assert resp.status_code == 200


# --- A wrong code counts toward the same lockout as a wrong password -------


def test_wrong_login_code_is_counted_and_logged(clock, client, user, login, caplog):
    _enrol(client, user)
    clock.now = T0 + 35

    with caplog.at_level(logging.WARNING, logger=auth_service.logger.name):
        resp = login(client, "000000")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid MFA code"
    assert user.failed_login_count == 1
    assert user.locked_until is None
    assert "Invalid MFA code for user: analyst" in caplog.text


def test_login_without_a_code_is_the_challenge_and_is_not_counted(client, user, login):
    _enrol(client, user)

    resp = client.post(
        "/api/auth/login", json={"username_or_email": "analyst", "password": "pw"}
    )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "MFA code required"
    assert user.failed_login_count == 0
    client.counter.commit.assert_not_called()


def test_repeated_wrong_login_codes_lock_the_account(client, user, login, monkeypatch):
    monkeypatch.setattr(auth_service, "LOCKOUT_THRESHOLD", 2)
    _enrol(client, user)

    first = login(client, "000000")
    second = login(client, "000001")

    assert first.status_code == 401
    assert second.status_code == 423
    assert (
        second.json()["detail"]
        == "Account locked due to repeated failed login attempts"
    )
    assert second.headers["retry-after"]
    assert user.failed_login_count == 2
    assert user.locked_until is not None


def test_mfa_failure_releases_the_row_lock_before_counting(client, user, login):
    """The counter commits on its own connection. It has to run after the
    request transaction lets go of the user row, or the two deadlock."""
    _enrol(client, user)
    order = []
    client.session.rollback.side_effect = lambda: order.append("rollback")
    real_get = uow_module.get_db_session

    def _counter_session():
        order.append("counter")
        return real_get()

    # client fixture patched get_db_session; wrap that patch.
    uow_module.get_db_session = _counter_session
    try:
        assert login(client, "000000").status_code == 401
    finally:
        uow_module.get_db_session = real_get

    assert order == ["rollback", "counter"]


def test_wrong_verify_code_counts_and_missing_setup_does_not(client, user):
    resp = client.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert resp.status_code == 400
    assert user.failed_login_count == 0

    secret = client.post("/api/auth/mfa/setup").json()["secret"]
    assert secret
    resp = client.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert resp.status_code == 400
    assert user.failed_login_count == 1
    assert user.mfa_enabled is False


def test_wrong_disable_code_counts_and_disabled_mfa_does_not(client, user, monkeypatch):
    monkeypatch.setattr(auth_service, "LOCKOUT_THRESHOLD", 2)
    assert (
        client.request("DELETE", "/api/auth/mfa", json={"code": "000000"}).status_code
        == 400
    )
    assert user.failed_login_count == 0

    _enrol(client, user)
    first = client.request("DELETE", "/api/auth/mfa", json={"code": "000000"})
    second = client.request("DELETE", "/api/auth/mfa", json={"code": "000001"})

    assert first.status_code == 400
    assert second.status_code == 423
    assert user.mfa_enabled is True
    assert user.failed_login_count == 2


def test_wrong_mfa_counts_persisted_failures_not_the_uncommitted_reset(monkeypatch):
    """Password success zeros the counter in the request transaction. A wrong
    code rolls that back; the lockout write must read the committed total."""
    caller_user = SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        is_active=True,
        password_hash="hashed",
        failed_login_count=4,
        locked_until=None,
        login_count=0,
        mfa_enabled=True,
    )
    persisted = SimpleNamespace(
        user_id="u-1",
        username="analyst",
        failed_login_count=4,
        locked_until=None,
    )
    caller = MagicMock()
    caller.query.return_value.filter.return_value.first.return_value = caller_user
    counter = MagicMock()
    counter.query.return_value.filter.return_value.first.return_value = persisted
    monkeypatch.setattr(uow_module, "get_db_session", lambda: counter)
    monkeypatch.setattr(
        auth.AuthService, "verify_password", staticmethod(lambda *a, **k: True)
    )
    monkeypatch.setattr(
        auth.AuthService, "verify_mfa_code", staticmethod(lambda *a, **k: False)
    )

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[request_unit_of_work] = lambda: caller
    limiter.reset()
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": "analyst", "password": "pw", "mfa_code": "000000"},
    )

    assert resp.status_code == 423
    assert persisted.failed_login_count == 5
    assert persisted.locked_until is not None
    # The request's copy was zeroed, and that zero must not have been committed.
    assert caller_user.failed_login_count == 0
    caller.commit.assert_not_called()
    caller.rollback.assert_called()
    counter.commit.assert_called_once()
