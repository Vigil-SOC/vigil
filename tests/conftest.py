# The suite runs with authentication ON, as a fresh install does — forced, not
# defaulted, so a DEV_MODE=true exported in a developer's shell cannot turn the
# auth checks into no-ops. auth_service raises at import time without a JWT
# secret once DEV_MODE is off, and API tests import routers at module load, so
# both are pinned before any test module imports. VIGIL_DISABLE_DOTENV likewise:
# import-time get_settings() captures run during collection, before the autouse
# fixture can neutralize env_file.

import os
from unittest.mock import patch

os.environ["DEV_MODE"] = "false"
# `or`, not setdefault: a shell that sourced .env exports JWT_SECRET_KEY="".
os.environ["JWT_SECRET_KEY"] = (
    os.environ.get("JWT_SECRET_KEY") or "test-only-secret-not-for-prod"
)
os.environ["VIGIL_DISABLE_DOTENV"] = "1"

import pytest  # noqa: E402


# get_settings() is lru_cached, so a test that monkeypatches env needs the cache
# dropped on both sides to avoid leaking a stale Settings.
@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from core.config import Settings, get_settings  # lazy: AST tests need no app deps

    Settings.model_config["env_file"] = None  # ignore the developer's .env, as CI does
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def authenticate_app():
    """Make a FastAPI app treat every request as a permitted admin.

    ``authenticate_app(app)`` overrides the session-auth dependencies on that
    app and returns the stand-in user; the override is removed at teardown.
    Overriding alone is not enough: ``services/api/middleware/auth.py`` resolves
    permissions by ``user_id`` against the database, and a synthetic user has
    none, so ``check_permission`` is patched to allow for the test's duration.
    The real cookie/JWT path is exercised by tests/security/ and the bootstrap
    test in tests/integration/.
    """
    # lazy: AST tests need no app deps
    from core.storage.models import User
    from services.api.middleware.auth import get_current_active_user, get_current_user

    user = User(
        user_id="test-admin",
        username="test-admin",
        email="admin@test.local",
        password_hash="",
        full_name="Test Admin",
        role_id="role-admin",
        is_active=True,
        mfa_enabled=False,
    )
    apps = []

    def _apply(app):
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_active_user] = lambda: user
        apps.append(app)
        return user

    with patch(
        "core.auth.auth_service.AuthService.check_permission", return_value=True
    ):
        yield _apply

    for app in apps:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_active_user, None)
