# DEV_MODE=true before any test module imports: API tests import routers at module
# load, and auth_service raises at import time without DEV_MODE or JWT_SECRET_KEY.

import os

os.environ.setdefault("DEV_MODE", "true")

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
