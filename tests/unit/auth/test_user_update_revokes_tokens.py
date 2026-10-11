"""Changing a user's email or role through PUT /api/users/{id} signs them out."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.api.routers import users


def _session(user, role):
    session = MagicMock()

    def query(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = user if model is users.User else role
        return q

    session.query.side_effect = query
    return session


def _user(**kw):
    return SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        role_id="role-analyst",
        full_name="A",
        is_active=True,
        is_verified=True,
        **kw,
    )


@pytest.fixture(autouse=True)
def _allow():
    with (
        patch.object(users.AuthService, "check_permission", return_value=True),
        patch.object(users, "_can_assign_role", return_value=True),
        patch.object(users.UserSchema, "dump", return_value={}),
    ):
        yield


def _apply(user, **req):
    session = _session(user, SimpleNamespace(role_id=req.get("role_id")))
    return users._apply_user_update(
        session, _user(), user.user_id, users.UpdateUserRequest(**req)
    )


def test_role_change_makes_tokens_stale():
    _, reason, _ = _apply(_user(), role_id="role-viewer")
    assert reason == "Role changed"


def test_same_role_does_not():
    _, reason, _ = _apply(_user(), role_id="role-analyst")
    assert reason is None


def test_name_change_does_not():
    _, reason, _ = _apply(_user(), full_name="B")
    assert reason is None


def test_update_user_revokes_on_role_change():
    target = _user()
    with (
        patch.object(
            users, "_apply_user_update", return_value=(target, "Role changed", {})
        ),
        patch.object(users, "revoke_all_for_user", new=AsyncMock()) as revoke,
    ):
        asyncio.run(
            users.update_user(
                "u-1",
                users.UpdateUserRequest(role_id="role-viewer"),
                _user(),
                MagicMock(),
            )
        )
    revoke.assert_awaited_once_with("u-1")


def test_update_user_leaves_tokens_alone_otherwise():
    target = _user()
    with (
        patch.object(users, "_apply_user_update", return_value=(target, None, {})),
        patch.object(users, "revoke_all_for_user", new=AsyncMock()) as revoke,
    ):
        asyncio.run(
            users.update_user(
                "u-1", users.UpdateUserRequest(full_name="B"), _user(), MagicMock()
            )
        )
    revoke.assert_not_awaited()
