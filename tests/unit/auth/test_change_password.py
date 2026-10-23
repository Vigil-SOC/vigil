"""POST /api/auth/change-password: why a reused password is refused (#2181), and
a wrong current password is a 400, so the web client never refresh-and-retries it (#2182).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

CURRENT = "trombone-sunrise-ledger-88"
OLD = "harbor-violet-compass-41"
NEW = "glacier-thimble-orchard-73"


@pytest.fixture(scope="module")
def hashes():
    # bcrypt is slow; hash once for the module.
    return AuthService.hash_password(CURRENT), AuthService.hash_password(OLD)


@pytest.fixture
def user(hashes):
    return SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        password_hash=hashes[0],
        password_history=[hashes[1]],
        password_changed_at=None,
    )


@pytest.fixture
def client(user):
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: MagicMock()
    limiter.reset()  # every test starts with a full budget
    with patch.object(auth, "revoke_all_for_user", new=AsyncMock()):
        yield TestClient(app)


def _change(client, new, current=CURRENT):
    return client.post(
        "/api/auth/change-password",
        json={"current_password": current, "new_password": new},
    )


@pytest.mark.parametrize("reused", [OLD, CURRENT])
def test_reused_password_is_400_with_reason(client, user, reused):
    before = user.password_hash
    resp = _change(client, reused)
    assert resp.status_code == 400
    assert "last" in resp.json()["detail"] and "passwords" in resp.json()["detail"]
    assert user.password_hash == before


def test_fresh_password_still_changes(client, user):
    resp = _change(client, NEW)
    assert resp.status_code == 200
    assert AuthService.verify_password(NEW, user.password_hash)
    assert len(user.password_history) == 2
    assert user.tokens_valid_after is not None


def test_password_change_stands_when_redis_revocation_fails(client, user):
    with patch.object(
        auth, "revoke_all_for_user", new=AsyncMock(side_effect=ConnectionError("OOM"))
    ):
        resp = _change(client, NEW)
    assert resp.status_code == 200
    assert resp.json()["message"] == (
        "Password changed successfully. Please log in again."
    )
    assert AuthService.verify_password(NEW, user.password_hash)
    assert user.tokens_valid_after is not None


def test_password_reset_stands_when_redis_revocation_fails(client, user):
    user.is_active = True
    with (
        patch.object(
            auth, "verify_reset_token", new=AsyncMock(return_value=user.user_id)
        ),
        patch.object(auth, "_get_user", return_value=user),
        patch.object(
            auth,
            "revoke_all_for_user",
            new=AsyncMock(side_effect=ConnectionError("OOM")),
        ),
    ):
        resp = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": "reset-token", "new_password": NEW},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Password reset successfully"
    assert AuthService.verify_password(NEW, user.password_hash)
    assert user.tokens_valid_after is not None


def test_wrong_current_password_is_400_and_leaves_hash(client, user):
    before = user.password_hash
    resp = _change(client, NEW, current="not-it")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is incorrect"
    assert user.password_hash == before


def test_five_wrong_attempts_before_429(client):
    codes = [_change(client, NEW, current="nope").status_code for _ in range(6)]
    assert codes == [400] * 5 + [429]
