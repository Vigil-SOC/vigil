"""What a service account can't be, and who can't make one (D2-17).

- No API sets ``users.service_account``: only ``core/auth/service_account.py``,
  which the enable flow runs inside the backend's environment. Otherwise any
  admin (or a stolen admin session) could exempt an account from lockout.
- The users API won't give a service account any role but Viewer, so the
  gateway's login can never read more than a Viewer does.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from services.api.routers import users as users_router

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
WRITER = REPO / "core" / "auth" / "service_account.py"


def _python_files():
    for root in ("core", "services"):
        for path in (REPO / root).rglob("*.py"):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            # Medic is its own process with its own models (A3-1).
            if path.is_relative_to(REPO / "services" / "medic"):
                continue
            yield path


def test_only_the_service_account_module_writes_the_flag():
    writers = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "service_account"
                ):
                    writers.append(f"{path.relative_to(REPO)}:{node.lineno}")
            # User(service_account=...) or .update({"service_account": ...})
            if isinstance(node, ast.keyword) and node.arg == "service_account":
                writers.append(f"{path.relative_to(REPO)}:{node.value.lineno}")
            if isinstance(node, ast.Constant) and node.value == "service_account":
                writers.append(f"{path.relative_to(REPO)}:{node.lineno}")
    allowed = str(WRITER.relative_to(REPO))
    assert writers, "the scan found nothing: it no longer sees the writer"
    assert {w.rsplit(":", 1)[0] for w in writers} == {allowed}, writers


def test_no_request_model_accepts_the_flag():
    """The API's own models: an extra field is ignored, never bound."""
    offenders = []
    for path in (REPO / "services" / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "service_account":
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == []

    body = {
        "username": "someone",
        "email": "someone@example.com",
        "password": "x",
        "full_name": "x",
        "role_id": "role-viewer",
        "service_account": True,
    }
    created = users_router.CreateUserRequest(**body)
    updated = users_router.UpdateUserRequest(service_account=True)
    for parsed in (created, updated):
        assert isinstance(parsed, BaseModel)
        assert "service_account" not in parsed.model_dump()


def _session_with(user, role_id: str):
    role = MagicMock()
    role.role_id = role_id
    role.permissions = {}
    session = MagicMock()

    def query(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = (
            user if model is users_router.User else role
        )
        return q

    session.query.side_effect = query
    return session


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(
        users_router.AuthService, "check_permission", staticmethod(lambda *a, **k: True)
    )
    monkeypatch.setattr(users_router, "_can_assign_role", lambda *a, **k: True)
    current = MagicMock()
    current.username = "admin"
    return current


def _target(service_account: bool):
    return users_router.User(
        user_id="user-1",
        username="medic-a1b2c3d4e5f6" if service_account else "someone",
        email="someone@example.com",
        full_name="x",
        role_id="role-viewer",
        service_account=service_account,
    )


@pytest.mark.parametrize("role_id", ["role-analyst", "role-manager", "role-admin"])
def test_role_change_above_viewer_is_refused_for_a_service_account(admin, role_id):
    user = _target(service_account=True)
    session = _session_with(user, role_id)

    with pytest.raises(HTTPException) as exc:
        users_router._apply_role_change(session, admin, "user-1", role_id)

    assert exc.value.status_code == 403
    assert user.role_id == "role-viewer"


@pytest.mark.parametrize("role_id", ["role-analyst", "role-admin"])
def test_update_to_a_role_above_viewer_is_refused_for_a_service_account(admin, role_id):
    user = _target(service_account=True)
    session = _session_with(user, role_id)
    request = users_router.UpdateUserRequest(role_id=role_id)

    with pytest.raises(HTTPException) as exc:
        users_router._apply_user_update(session, admin, "user-1", request)

    assert exc.value.status_code == 403
    assert user.role_id == "role-viewer"


def test_a_normal_user_can_still_be_promoted(admin):
    user = _target(service_account=False)
    session = _session_with(user, "role-analyst")

    users_router._apply_role_change(session, admin, "user-1", "role-analyst")

    assert user.role_id == "role-analyst"


def test_email_change_is_refused_for_a_service_account(admin):
    """A changed email is the first step to a password reset (review N5)."""
    user = _target(service_account=True)
    session = _session_with(user, "role-viewer")
    request = users_router.UpdateUserRequest(email="attacker@example.com")

    with pytest.raises(HTTPException) as exc:
        users_router._apply_user_update(session, admin, "user-1", request)

    assert exc.value.status_code == 403
    assert user.email == "someone@example.com"
