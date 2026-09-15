# The suite runs the way an install runs: authenticated. What the bypass was
# actually covering here is auth_service raising at import without a signing
# secret, so supply a secret instead of turning authentication off.
#
# Both must be set before any test module imports: API tests import routers at
# module load. VIGIL_DISABLE_DOTENV likewise — import-time get_settings()
# captures run during collection, before the autouse fixture can neutralize
# env_file.

import os

os.environ.setdefault("DEV_MODE", "false")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-signing-secret-not-for-any-real-use")
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


# --- authenticated API client ------------------------------------------------
#
# The suite runs with authentication on, so any test driving an authed route has
# to present a user. Two things are needed, not one: overriding the dependency
# gets past the 401, and the RBAC check behind it is a database lookup by
# user_id that a synthetic user cannot satisfy — without patching it the same
# requests turn into 403s instead.
@pytest.fixture
def authenticate_app():
    """Return ``authenticate(app)``: a context manager signing requests in as admin.

    Use it around requests a route protects::

        with authenticate_app(client.app):
            client.put("/api/mcp/servers/x/enabled", json={"enabled": True})
    """
    import uuid
    from contextlib import contextmanager

    @contextmanager
    def _authenticate(app, *, permissions: bool = True):
        from core.auth.auth_service import AuthService
        from core.storage.models import User
        from services.api.middleware.auth import (
            get_current_active_user,
            get_current_user,
        )

        # Transient and never added to a session, so nothing expires under it.
        admin = User(
            user_id=str(uuid.uuid4()),
            username="test-admin",
            email="test-admin@localhost",
            password_hash="",
            role_id="role-admin",
            is_active=True,
            mfa_enabled=False,
        )

        original = AuthService.check_permission
        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_current_active_user] = lambda: admin
        AuthService.check_permission = staticmethod(lambda *a, **k: permissions)
        try:
            yield admin
        finally:
            AuthService.check_permission = original
            app.dependency_overrides.pop(get_current_user, None)
            app.dependency_overrides.pop(get_current_active_user, None)

    return _authenticate
