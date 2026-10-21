"""Self-service MFA routes: setup is refused while enabled (409), and turning
MFA off needs a current TOTP or recovery code and clears the stored codes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import core.auth.auth_service as auth_service
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
    )


@pytest.fixture
def client(user):
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.populate_existing.return_value.with_for_update.return_value = query
    query.first.return_value = user
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: session
    limiter.reset()
    return TestClient(app)


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


def test_disable_is_rate_limited(client, user):
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
