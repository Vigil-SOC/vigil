# DEV_MODE=true before any test module imports: API tests import routers at module
# load, and auth_service raises at import time without DEV_MODE or JWT_SECRET_KEY.
# VIGIL_DISABLE_DOTENV likewise: import-time get_settings() captures run during
# collection, before the autouse fixture can neutralize env_file.

import os

os.environ.setdefault("DEV_MODE", "true")
os.environ["VIGIL_DISABLE_DOTENV"] = "1"

# The CSRF settings an unconfigured install has -- `core/config.py` defaults
# them to exactly this. Stated here rather than left to the defaults because a
# developer's shell can carry either one, and stated once rather than by
# whichever test module happens to be collected first: four modules used to set
# `VIGIL_CSRF_ENABLED=false` at import, process-wide and without cleanup, so
# what the suite ran under depended on the order pytest walked it in. Enabled
# and report-only means a violation is logged and the request proceeds, which
# is why a clean checkout has never needed the flag.
os.environ["VIGIL_CSRF_ENABLED"] = "true"
os.environ["VIGIL_CSRF_REPORT_ONLY"] = "true"

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
