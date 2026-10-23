"""
Unit tests for core.auth.password_reset.

Redis-backed single-use enforcement is mocked so tests run without a live
cache. The signature + expiry logic is pure itsdangerous and runs as-is.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import password_reset
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth


@pytest.fixture(autouse=True)
def _mock_redis(monkeypatch):
    """Default every test to a mock Redis that records SET NX as success.
    Individual tests override as needed."""
    client = AsyncMock()
    client.set = AsyncMock(return_value=True)
    monkeypatch.setattr(password_reset, "_redis_client", lambda: client)
    return client


class TestPasswordReset:
    def test_token_roundtrip_returns_user_id(self):
        token = password_reset.generate_reset_token("user-abc")
        assert password_reset.decode_reset_token(token) == "user-abc"

    def test_tampered_token_is_rejected(self):
        token = password_reset.generate_reset_token("user-abc")
        tampered = token[:-2] + ("A" if token[-1] != "A" else "B") + "="
        assert password_reset.decode_reset_token(tampered) is None

    def test_token_for_one_user_does_not_unlock_another(self):
        token_a = password_reset.generate_reset_token("user-a")
        token_b = password_reset.generate_reset_token("user-b")
        assert password_reset.decode_reset_token(token_a) == "user-a"
        assert password_reset.decode_reset_token(token_b) == "user-b"

    def test_replay_is_rejected_when_redis_reports_used(self, _mock_redis):
        _mock_redis.set = AsyncMock(side_effect=[True, False])
        token = password_reset.generate_reset_token("user-abc")
        assert asyncio.run(password_reset.consume_reset_token(token)) is True
        assert asyncio.run(password_reset.consume_reset_token(token)) is False

    def test_redis_outage_is_fail_open(self, monkeypatch):
        client = AsyncMock()
        client.set = AsyncMock(side_effect=RuntimeError("redis down"))
        monkeypatch.setattr(password_reset, "_redis_client", lambda: client)
        token = password_reset.generate_reset_token("user-abc")
        assert asyncio.run(password_reset.consume_reset_token(token)) is True

    def test_expired_token_is_rejected(self, monkeypatch):
        # Force an already-elapsed TTL. itsdangerous raises SignatureExpired
        # only when age > max_age, so a TTL of 0 leaves a freshly-minted token
        # valid for its first second (age 0 is not > 0). A negative TTL makes
        # any token deterministically expired regardless of timing.
        monkeypatch.setattr(password_reset, "_ttl_seconds", lambda: -1)
        token = password_reset.generate_reset_token("user-abc")
        assert password_reset.decode_reset_token(token) is None


OLD = "harbor-violet-compass-41"
NEW = "glacier-thimble-orchard-73"


class _NxRedis:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


class TestConfirm:
    @pytest.fixture
    def user(self):
        return SimpleNamespace(
            user_id="u-1",
            username="analyst",
            email="a@example.com",
            is_active=True,
            password_hash=AuthService.hash_password(OLD),
            password_history=[],
            password_changed_at=None,
        )

    @pytest.fixture
    def client(self, user, monkeypatch):
        redis = _NxRedis()
        monkeypatch.setattr(password_reset, "_redis_client", lambda: redis)
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(auth.router, prefix="/api/auth")
        app.dependency_overrides[request_unit_of_work] = lambda: MagicMock()
        limiter.reset()
        with (
            patch.object(auth, "_get_user", return_value=user),
            patch.object(auth, "revoke_all_for_user", new=AsyncMock()) as revoke,
        ):
            yield TestClient(app), revoke

    @staticmethod
    def _confirm(client, token, password):
        return client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": password},
        )

    @pytest.mark.parametrize("rejected", ["short", OLD])
    def test_rejected_password_leaves_the_token_usable(self, client, user, rejected):
        client, _ = client
        token = password_reset.generate_reset_token("u-1")
        assert self._confirm(client, token, rejected).status_code == 400
        resp = self._confirm(client, token, NEW)
        assert resp.status_code == 200, resp.text
        assert AuthService.verify_password(NEW, user.password_hash)

    def test_accepted_token_cannot_be_replayed(self, client):
        client, revoke = client
        token = password_reset.generate_reset_token("u-1")
        assert self._confirm(client, token, NEW).status_code == 200
        resp = self._confirm(client, token, "orchid-lantern-quarry-52")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid or expired reset token"
        assert revoke.await_count == 1
