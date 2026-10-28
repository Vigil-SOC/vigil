"""
Unit tests for core.auth.password_reset and the confirm route.

A token is bound to the user's password_changed_at, so single use needs no
cache. The signature + expiry logic is pure itsdangerous and runs as-is.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import password_reset
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth


def _user(user_id="user-abc", changed_at=None):
    return SimpleNamespace(user_id=user_id, password_changed_at=changed_at)


class TestPasswordReset:
    def test_token_roundtrip_returns_user_id(self):
        user = _user()
        claim = password_reset.decode_reset_token(
            password_reset.generate_reset_token(user)
        )
        assert claim.user_id == "user-abc"
        # A never-changed password (NULL) still gets a working link.
        assert password_reset.is_current(claim, user)

    def test_tampered_token_is_rejected(self):
        token = password_reset.generate_reset_token(_user())
        tampered = token[:-2] + ("A" if token[-1] != "A" else "B") + "="
        assert password_reset.decode_reset_token(tampered) is None

    def test_token_for_one_user_does_not_unlock_another(self):
        claim = password_reset.decode_reset_token(
            password_reset.generate_reset_token(_user("user-a"))
        )
        assert not password_reset.is_current(claim, _user("user-b"))

    def test_password_change_voids_the_token(self):
        user = _user(changed_at=datetime(2026, 1, 1, 12, 0, 0, 1))
        claim = password_reset.decode_reset_token(
            password_reset.generate_reset_token(user)
        )
        assert password_reset.is_current(claim, user)
        # Same second, one microsecond later: still a different stamp.
        user.password_changed_at = datetime(2026, 1, 1, 12, 0, 0, 2)
        assert not password_reset.is_current(claim, user)

    def test_token_without_stamp_is_rejected(self):
        # Shape of a token issued before the stamp was added.
        token = password_reset._get_serializer().dumps({"user_id": "user-abc"})
        assert password_reset.decode_reset_token(token) is None

    def test_expired_token_is_rejected(self, monkeypatch):
        # Force an already-elapsed TTL. itsdangerous raises SignatureExpired
        # only when age > max_age, so a TTL of 0 leaves a freshly-minted token
        # valid for its first second (age 0 is not > 0). A negative TTL makes
        # any token deterministically expired regardless of timing.
        monkeypatch.setattr(password_reset, "_ttl_seconds", lambda: -1)
        token = password_reset.generate_reset_token(_user())
        assert password_reset.decode_reset_token(token) is None


OLD = "harbor-violet-compass-41"
NEW = "glacier-thimble-orchard-73"


class TestConfirm:
    @pytest.fixture
    def user(self):
        return SimpleNamespace(
            user_id="u-1",
            principal_id="u-1",
            username="analyst",
            email="a@example.com",
            is_active=True,
            password_hash=AuthService.hash_password(OLD),
            password_history=[],
            password_changed_at=None,
            service_account=False,
        )

    @pytest.fixture
    def revoke(self):
        return AsyncMock()

    @pytest.fixture
    def client(self, user, revoke):
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(auth.router, prefix="/api/auth")
        app.dependency_overrides[request_unit_of_work] = lambda: MagicMock()
        app.dependency_overrides[get_current_active_user] = lambda: user
        limiter.reset()
        with (
            patch.object(auth, "_get_user", return_value=user),
            patch.object(auth, "revoke_all_for_user", new=revoke),
        ):
            yield TestClient(app)

    @staticmethod
    def _confirm(client, token, password):
        return client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": password},
        )

    @staticmethod
    def _rejected(resp):
        return (
            resp.status_code == 400
            and resp.json()["detail"] == "Invalid or expired reset token"
        )

    @pytest.mark.parametrize("rejected", ["short", OLD])
    def test_rejected_password_leaves_the_token_usable(self, client, user, rejected):
        token = password_reset.generate_reset_token(user)
        assert self._confirm(client, token, rejected).status_code == 400
        resp = self._confirm(client, token, NEW)
        assert resp.status_code == 200, resp.text
        assert AuthService.verify_password(NEW, user.password_hash)

    def test_accepted_token_cannot_be_replayed(self, client, user, revoke):
        token = password_reset.generate_reset_token(user)
        assert self._confirm(client, token, NEW).status_code == 200
        assert self._rejected(self._confirm(client, token, "orchid-lantern-quarry-52"))
        assert revoke.await_count == 1
        assert AuthService.verify_password(NEW, user.password_hash)

    def test_completed_reset_voids_earlier_links(self, client, user):
        first = password_reset.generate_reset_token(user)
        second = password_reset.generate_reset_token(user)
        assert self._confirm(client, second, NEW).status_code == 200
        assert self._rejected(self._confirm(client, first, "orchid-lantern-quarry-52"))
        assert AuthService.verify_password(NEW, user.password_hash)

    def test_password_change_voids_the_link(self, client, user):
        token = password_reset.generate_reset_token(user)
        resp = client.post(
            "/api/auth/change-password",
            json={"current_password": OLD, "new_password": NEW},
        )
        assert resp.status_code == 200, resp.text
        assert self._rejected(self._confirm(client, token, "orchid-lantern-quarry-52"))
        assert AuthService.verify_password(NEW, user.password_hash)

    def test_link_works_once_while_redis_is_down(self, client, user, revoke):
        # Redis is only touched for session revocation now; its outage
        # neither blocks a fresh link nor lets a used one through again.
        revoke.side_effect = ConnectionError("redis down")
        token = password_reset.generate_reset_token(user)
        assert self._confirm(client, token, NEW).status_code == 200
        assert self._rejected(self._confirm(client, token, "orchid-lantern-quarry-52"))
        assert AuthService.verify_password(NEW, user.password_hash)
