"""A wrong current password is a 400, so the web client never refresh-and-retries it."""

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
from services.api.routers import auth

OLD = "Old-Passw0rd!xyz"
NEW = "Brand-New-Passw0rd!42"


@pytest.fixture
def ctx():
    user = SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        password_hash=AuthService.hash_password(OLD),
        password_history=[],
        password_changed_at=None,
    )
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: MagicMock()
    app.state.limiter = auth.limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    auth.limiter.reset()  # every test starts with a full budget
    with patch.object(auth, "revoke_all_for_user", new=AsyncMock()):
        yield TestClient(app), user


def _post(client, current):
    return client.post(
        "/api/auth/change-password",
        json={"current_password": current, "new_password": NEW},
    )


def test_wrong_current_password_is_400_and_leaves_hash(ctx):
    client, user = ctx
    before = user.password_hash
    r = _post(client, "not-it")
    assert r.status_code == 400
    assert r.json()["detail"] == "Current password is incorrect"
    assert user.password_hash == before


def test_right_current_password_changes_it(ctx):
    client, user = ctx
    assert _post(client, OLD).status_code == 200
    assert AuthService.verify_password(NEW, user.password_hash)


def test_five_wrong_attempts_before_429(ctx):
    client, _ = ctx
    assert [_post(client, "nope").status_code for _ in range(6)] == [400] * 5 + [429]
