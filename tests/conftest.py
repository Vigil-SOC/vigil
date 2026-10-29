# The suite runs with authentication ON, as a fresh install does — forced, not
# defaulted, so a DEV_MODE=true exported in a developer's shell cannot turn the
# auth checks into no-ops. auth_service raises at import time without a JWT
# secret once DEV_MODE is off, and API tests import routers at module load, so
# both are pinned before any test module imports. VIGIL_DISABLE_DOTENV likewise:
# import-time get_settings() captures run during collection, before the autouse
# fixture can neutralize env_file.

import os

os.environ["DEV_MODE"] = "false"
# `or`, not setdefault: a shell that sourced .env exports JWT_SECRET_KEY="".
os.environ["JWT_SECRET_KEY"] = (
    os.environ.get("JWT_SECRET_KEY") or "test-only-secret-not-for-prod"
)
os.environ["VIGIL_DISABLE_DOTENV"] = "1"

# The CSRF settings the suite runs under. Not the product default: an
# unconfigured install enforces (`core/config.py`), but most tests POST without
# a csrf_token pair, so the suite pins report-only -- a violation is logged and
# the request proceeds. Stated once rather than by whichever test module
# happens to be collected first: three modules used to set
# `VIGIL_CSRF_ENABLED=false` at import, process-wide and without cleanup, so
# what the suite ran under depended on the order pytest walked it in.
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


@pytest.fixture(autouse=True)
def _no_ambient_postgres(monkeypatch):
    """Point any accidental DB connection at a host that cannot resolve (#1456).

    Without this a test falls back to localhost:5432 and the built-in password,
    i.e. a developer's own running stack.
    """
    monkeypatch.setenv("POSTGRES_HOST", "postgres-blocked-in-unit-tests.invalid")
    monkeypatch.setenv("POSTGRES_PORT", "1")
    monkeypatch.setenv("POSTGRES_PASSWORD", "blocked-in-unit-tests")
    monkeypatch.delenv("POSTGRESQL_CONNECTION_STRING", raising=False)
    try:
        # A developer's stored DSN would otherwise outrank the POSTGRES_* pin.
        monkeypatch.setattr(
            "core.storage.connection._load_connection_string_secret", lambda: None
        )
        from core.config import get_settings

        get_settings.cache_clear()
    except ImportError:  # AST-only environments have no app deps
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_llm_path_cache():
    """One snapshot serves every route. Drop it so a case cannot answer the next."""
    import sys

    mod = sys.modules.get("core.llm.bifrost.path_health")
    if mod is not None:
        mod.reset()
    yield
    mod = sys.modules.get("core.llm.bifrost.path_health")
    if mod is not None:
        mod.reset()


@pytest.fixture(autouse=True)
def _bound_to_a_run(request):
    """A tool a test calls directly runs as a run no person started.

    Nothing bound is refused (#3306), and invoke always binds someone. A test
    of that refusal marks itself ``unbound``.
    """
    if request.node.get_closest_marker("unbound"):
        yield
        return
    try:
        from core.auth.tool_principal import Claims, bound_claims
    except ImportError:  # a suite that runs without core installed
        yield
        return

    run = Claims(
        username=None,
        person_id=None,
        agent_id="agent:hunt",
        run_id="run-test",
        origin="schedule",
    )
    with bound_claims(run):
        yield
