"""A throwaway State Directory, and a signed-in operator allowed to write skills."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from services.api.routers import skills as skills_router


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """The State Directory a default skills root resolves under. Not created."""
    state = tmp_path / "state"
    monkeypatch.setenv("VIGIL_DIR", str(state))
    return state


@pytest.fixture(autouse=True)
def _may_write_skills(monkeypatch):
    """Every permission granted; the role tests patch this back per role."""
    monkeypatch.setattr(AuthService, "check_permission", lambda *_a, **_k: True)


def skills_app() -> FastAPI:
    """The skills router alone, with a signed-in user in front of its gates."""
    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/skills")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="operator"
    )
    return app
