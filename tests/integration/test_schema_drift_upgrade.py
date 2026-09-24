"""Schema drift on ORM-only tables — the upgrade path, not the fresh-build path.

CI only ever builds from an empty database, which is exactly why #562 was
invisible: `create_all` produces a complete schema from scratch, so a column
added to a model looks fine in CI and is missing on every existing deployment.

These tests provision a database, move the *model* forward relative to it, and
assert on what an upgrade actually does. Three current behaviours are pinned as
characterisation tests — `create_all` will not alter an existing table, the
detector notices, the ORM then raises — so that a regression in any of them is
visible. The rest cover the startup check added for #562.

The fixtures CREATE and DROP a scratch database on whatever the POSTGRES_*
variables point at, so they refuse to run against a non-local host.
"""

import logging
import os
import time

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

from core.config import get_settings
from core.storage import connection as conn
from core.storage.connection import (
    DatabaseManager,
    SchemaDriftError,
    check_schema_drift,
    get_schema_drift_report,
    reset_schema_drift_check,
)
from core.storage.models import Base, CaseWatcher

pytestmark = [pytest.mark.integration, pytest.mark.database]

# An ORM-only table: no CREATE TABLE for it exists anywhere in
# infra/database/init/ or the Helm bundle, so create_all is the only thing that
# can build it and the numbered-init-file mechanism cannot reach it at all.
DRIFT_TABLE = "case_watchers"
DRIFT_COLUMN = "notification_preferences"

SCRATCH_DB = "vigil_test_schema_drift"

# These fixtures DROP DATABASE. Only ever against a loopback host — a developer
# whose POSTGRES_HOST points at a shared or staging server must get a skip, not
# a dropped database.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"}

# Bounded so an unreachable-but-routable host fails in seconds rather than
# hanging on the OS TCP timeout (~75s), matching DatabaseManager._build.
_CONNECT_ARGS = {"connect_timeout": 5}


def _host() -> str:
    return os.getenv("POSTGRES_HOST", "localhost")


def _url(database: str) -> str:
    """Build a DSN from the POSTGRES_* variables CI sets, else dev defaults.

    CI's integration job uses test/test/deeptempo_test; a developer's compose
    stack uses deeptempo/deeptempo_secure_password_change_me. Hardcoding either
    makes the test silently skip in the other environment.
    """
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{_host()}:{port}/{database}"


# 'postgres' always exists and is never the target, so it is safe to CREATE and
# DROP the scratch database from.
def _admin_engine():
    return create_engine(
        _url("postgres"), isolation_level="AUTOCOMMIT", connect_args=_CONNECT_ARGS
    )


def _requires_local_postgres():
    """Skip reasons, evaluated per test rather than at import.

    Probing at module scope would open a connection during collection, so an
    unreachable host would stall the whole run before a single test starts.
    """
    if _host() not in _LOCAL_HOSTS:
        return (
            f"refusing to CREATE/DROP a database on non-local POSTGRES_HOST "
            f"{_host()!r}"
        )
    try:
        with _admin_engine().connect():
            return None
    except Exception as e:  # noqa: BLE001
        return f"requires a local PostgreSQL (docker compose up -d postgres): {e}"


@pytest.fixture(scope="session")
def postgres_available():
    reason = _requires_local_postgres()
    if reason:
        pytest.skip(reason)


@pytest.fixture
def drifted_db(postgres_available):
    """A database provisioned at 'version N', then rewound behind the model.

    Dropping the column is how we emulate a database that predates it: the
    deployed schema is one release behind what models.py now declares. This is
    the situation every existing deployment is in after a column is added.
    """
    admin = _admin_engine()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(_url(SCRATCH_DB), connect_args=_CONNECT_ARGS)
    with scratch.connect() as c:
        # create_all cannot provision from a bare database on its own: the
        # findings GIN index needs pg_trgm or it fails with
        # 'operator class gin_trgm_ops does not exist'.
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        c.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        c.commit()

    Base.metadata.create_all(scratch)
    with scratch.connect() as c:
        c.execute(text(f"ALTER TABLE {DRIFT_TABLE} DROP COLUMN {DRIFT_COLUMN}"))
        c.commit()

    yield scratch

    scratch.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _manager_for(engine, mp):
    """The DatabaseManager singleton, pointed at `engine` until `mp` is undone.

    `_engine` is assigned directly on purpose: DatabaseManager resolves its URL
    through the config layer (core/storage/connection.py:405) and ignores the
    DATABASE_URL environment variable, so setting that env var would silently
    inspect the developer's real database and report a healthy schema.

    The instance is process-wide, so the patch must be undone before the scratch
    database is dropped — otherwise every later test in the process connects to
    a database that no longer exists (#1051).
    """
    dm = DatabaseManager()
    mp.setattr(dm, "_engine", engine)
    mp.setattr(dm, "_session_factory", sessionmaker(bind=engine))
    return dm


@pytest.fixture
def drifted_manager(drifted_db):
    # Its own MonkeyPatch rather than the `monkeypatch` fixture: that one is
    # set up first (by the autouse fixture) and so undone after drifted_db
    # has already dropped the database.
    with pytest.MonkeyPatch.context() as mp:
        yield _manager_for(drifted_db, mp)


@pytest.fixture(autouse=True)
def _reset_drift_state(monkeypatch):
    """The startup check memoises its inspection; each test needs a clean slate.

    Two independent caches are involved. `reset_schema_drift_check()` clears the
    drift verdict, and `get_settings.cache_clear()` clears the Settings object —
    `get_settings` is `@lru_cache(maxsize=1)` (`core/config.py`), so a Settings
    instance built by any earlier fixture would otherwise pin the value of
    DB_STRICT_SCHEMA for the rest of the session.
    """
    monkeypatch.delenv("DB_STRICT_SCHEMA", raising=False)
    get_settings.cache_clear()
    reset_schema_drift_check()
    yield
    reset_schema_drift_check()
    get_settings.cache_clear()


def _set_strict(monkeypatch, value):
    """Set DB_STRICT_SCHEMA so that `check_schema_drift` will actually see it.

    Setting the variable alone is not enough: `DatabaseManager()` reads pool
    settings through `get_settings()`, which populates the lru_cache before the
    test body runs, so the patched value would be invisible. Dropping the cache
    after the patch is what makes the read happen against the patched env.
    """
    monkeypatch.setenv("DB_STRICT_SCHEMA", value)
    get_settings.cache_clear()


def _patch_report(monkeypatch, fn):
    """Replace schema_report on the class: an instance-level patch leaves the
    bound original behind on the singleton when undone."""
    monkeypatch.setattr(DatabaseManager, "schema_report", lambda self: fn())


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


# --------------------------------------------------------------------------
# Findings 1-3: what an upgrade does today. These must keep being true.
# --------------------------------------------------------------------------


def test_create_all_does_not_restore_a_missing_column(drifted_db):
    """create_all is checkfirst=True: it creates missing tables, never alters."""
    assert DRIFT_COLUMN not in _columns(drifted_db, DRIFT_TABLE)

    Base.metadata.create_all(drifted_db)  # what an app restart does

    assert DRIFT_COLUMN not in _columns(drifted_db, DRIFT_TABLE), (
        "create_all restored a dropped column — if this ever passes, the "
        "premise of #562 has changed and the startup check may be redundant"
    )


def test_create_all_reports_success_while_leaving_drift(drifted_db):
    """The silence is the bug: nothing distinguishes this from a good schema."""
    Base.metadata.create_all(drifted_db)  # must not raise


def test_schema_report_detects_the_missing_column(drifted_manager):
    report = drifted_manager.schema_report()

    assert report["state"] == "drifted"
    assert report["missing_tables"] == []
    assert report["missing_columns"].get(DRIFT_TABLE) == [DRIFT_COLUMN]


def test_schema_report_detects_a_not_null_the_models_dropped(
    drifted_db, drifted_manager
):
    """A database from before #1115 still forbids NULL cost, so every unpriced
    call's audit row would fail to insert; that must read as drift."""
    with drifted_db.begin() as c:
        c.execute(
            text("ALTER TABLE llm_interaction_logs ALTER COLUMN cost_usd SET NOT NULL")
        )

    report = drifted_manager.schema_report()

    assert report["state"] == "drifted"
    assert report["not_null_columns"] == {"llm_interaction_logs": ["cost_usd"]}


def test_orm_read_raises_even_when_the_table_is_empty(drifted_db):
    """The generated SELECT names every mapped column, so rows are irrelevant.

    This is the symptom an operator actually sees: a 500 on a column that is
    plainly present in models.py, with nothing pointing at schema drift.
    """
    with drifted_db.connect() as conn:
        rows = conn.execute(text(f"SELECT count(*) FROM {DRIFT_TABLE}")).scalar()
    assert rows == 0

    session = sessionmaker(bind=drifted_db)()
    try:
        with pytest.raises(ProgrammingError) as excinfo:
            session.query(CaseWatcher).first()
    finally:
        session.close()

    assert "does not exist" in str(excinfo.value)
    assert DRIFT_COLUMN in str(excinfo.value)


# --------------------------------------------------------------------------
# The #562 startup check.
# --------------------------------------------------------------------------


def test_drift_is_logged_at_error_with_the_exact_columns(drifted_manager, caplog):
    """Silent drift is the defect; an actionable ERROR is the fix."""
    with caplog.at_level(logging.ERROR):
        report = check_schema_drift(db_manager=drifted_manager)

    assert report["state"] == "drifted"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "drift must be reported at ERROR, not WARNING or below"

    logged = " ".join(r.getMessage() for r in errors)
    assert (
        f"{DRIFT_TABLE}.{DRIFT_COLUMN}" in logged
    ), f"the message must name the missing column, got: {logged!r}"
    assert "migrate_schema" in logged, "the message should say what to run"


def test_serving_continues_by_default(drifted_manager):
    """Taking a running SOC offline over a nullable column is worse than drift."""
    report = check_schema_drift(db_manager=drifted_manager)  # must not raise
    assert report["state"] == "drifted"


def test_strict_mode_refuses_to_start(drifted_manager, monkeypatch):
    _set_strict(monkeypatch, "true")

    with pytest.raises(SchemaDriftError) as excinfo:
        check_schema_drift(db_manager=drifted_manager)

    assert f"{DRIFT_TABLE}.{DRIFT_COLUMN}" in str(excinfo.value)


def test_strict_mode_refuses_every_time_not_just_once(drifted_manager, monkeypatch):
    """The inspection is memoised; the refusal must not be.

    DatabaseDataService._init_database() calls init_database() on construction
    and again on every reconnect attempt. A refusal that fires once and then
    reports the same drift quietly would let any caller that swallows the first
    exception carry on serving — which is the failure mode #562 is about.
    """
    _set_strict(monkeypatch, "true")

    for attempt in range(3):
        with pytest.raises(SchemaDriftError):
            check_schema_drift(db_manager=drifted_manager)


def test_strict_mode_refuses_an_unprovisioned_database(monkeypatch, postgres_available):
    """`empty` after create_all means create_all did nothing — worse than drift.

    schema_report() classifies a database with no Vigil tables as `empty` rather
    than `drifted`, so treating only `drifted` as a failure would let CI's
    strict mode pass against a database nothing had provisioned.
    """
    admin = _admin_engine()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    bare = create_engine(_url(SCRATCH_DB), connect_args=_CONNECT_ARGS)
    try:
        with pytest.MonkeyPatch.context() as mp:
            manager = _manager_for(bare, mp)
            assert manager.schema_report()["state"] == "empty"

            # A caller that has not provisioned yet is entitled to an empty
            # database.
            check_schema_drift(db_manager=manager, provisioned=False)

            _set_strict(monkeypatch, "true")
            reset_schema_drift_check()
            with pytest.raises(SchemaDriftError):
                check_schema_drift(db_manager=manager, provisioned=True)
    finally:
        bare.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "on", "y", "t"])
def test_strict_mode_accepts_pydantics_truthy_spellings(
    drifted_manager, monkeypatch, value
):
    """Parsing is pydantic's, the same as every other boolean in Settings."""
    _set_strict(monkeypatch, value)
    with pytest.raises(SchemaDriftError):
        check_schema_drift(db_manager=drifted_manager)


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "n", "f"])
def test_strict_mode_off_for_falsey_spellings(drifted_manager, monkeypatch, value):
    _set_strict(monkeypatch, value)
    check_schema_drift(db_manager=drifted_manager)  # must not raise


def test_healthy_schema_logs_no_error(drifted_db, drifted_manager, caplog):
    """No false alarms: a correct schema must stay quiet."""
    with drifted_db.connect() as c:
        c.execute(text(f"ALTER TABLE {DRIFT_TABLE} ADD COLUMN {DRIFT_COLUMN} JSONB"))
        c.commit()

    with caplog.at_level(logging.ERROR):
        report = check_schema_drift(db_manager=drifted_manager)

    assert report["state"] == "ok"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_report_is_readable_after_the_check_for_health_output(drifted_manager):
    """Health must read a cached verdict, never re-inspect per request.

    schema_report() walks every mapped table; doing that inside the health
    handler would put blocking I/O on the event loop for every scrape.
    """
    assert get_schema_drift_report() is None

    check_schema_drift(db_manager=drifted_manager)

    cached = get_schema_drift_report()
    assert cached is not None
    assert cached["state"] == "drifted"
    assert cached["missing_columns"].get(DRIFT_TABLE) == [DRIFT_COLUMN]


def test_inspection_runs_once_and_is_memoised(drifted_manager, monkeypatch):
    """init_database() is called on every DatabaseDataService construction —
    including from the health endpoint — so an unmemoised check would add a
    full inspector pass per request."""
    calls = []
    original = drifted_manager.schema_report

    def counting_report():
        calls.append(1)
        return original()

    _patch_report(monkeypatch, counting_report)

    check_schema_drift(db_manager=drifted_manager)
    check_schema_drift(db_manager=drifted_manager)
    check_schema_drift(db_manager=drifted_manager)

    assert len(calls) == 1, f"expected one inspection, got {len(calls)}"


def test_drift_is_logged_once_not_per_call(drifted_manager, caplog):
    """One ERROR at startup, not one per health scrape."""
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            check_schema_drift(db_manager=drifted_manager)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"expected a single ERROR, got {len(errors)}"


def test_a_failed_inspection_is_not_cached_as_checked(
    drifted_manager, caplog, monkeypatch
):
    """A transient failure must not disable the check for the process lifetime.

    DatabaseDataService retries _init_database() on reconnect, so caching a
    failure would mean one blip during startup silently switches the check off —
    including strict mode — until the process restarts.
    """
    boom = {"n": 0}
    original = drifted_manager.schema_report

    def failing_once():
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("database not initialized")
        return original()

    _patch_report(monkeypatch, failing_once)

    with caplog.at_level(logging.WARNING):
        assert check_schema_drift(db_manager=drifted_manager) is None
    assert get_schema_drift_report() is None

    report = check_schema_drift(db_manager=drifted_manager)
    assert report is not None and report["state"] == "drifted"


def test_an_uninspectable_schema_is_not_recorded_as_a_verdict(
    drifted_manager, monkeypatch
):
    """`unknown` is schema_report failing to inspect, not a healthy schema.

    schema_report() catches its own inspection errors and returns
    `state: unknown` rather than raising, so this — not an exception — is what a
    connection blip during startup actually looks like. Recording it would
    disable the check, strict mode included, until the process restarts.
    """
    original = drifted_manager.schema_report
    blips = {"n": 0}

    def unknown_once():
        blips["n"] += 1
        if blips["n"] == 1:
            return {"state": "unknown", "missing_tables": [], "missing_columns": {}}
        return original()

    _patch_report(monkeypatch, unknown_once)

    assert check_schema_drift(db_manager=drifted_manager) is None
    assert get_schema_drift_report() is None, "unknown must not become the verdict"

    report = check_schema_drift(db_manager=drifted_manager)
    assert report is not None and report["state"] == "drifted"


def test_a_repaired_schema_is_noticed_without_a_restart(
    drifted_db, drifted_manager, monkeypatch
):
    """An unhealthy verdict is provisional; the operator can fix it in place.

    Caching drift for the life of the process would mean _db_available's
    reconnect could never succeed again once strict mode had refused, and
    /api/health would keep reporting drift after the migration that fixed it.
    """
    monkeypatch.setattr(conn, "_SCHEMA_RECHECK_SECONDS", 0.0)
    manager = drifted_manager

    assert check_schema_drift(db_manager=manager)["state"] == "drifted"

    with drifted_db.connect() as c:
        c.execute(text(f"ALTER TABLE {DRIFT_TABLE} ADD COLUMN {DRIFT_COLUMN} JSONB"))
        c.commit()

    assert check_schema_drift(db_manager=manager)["state"] == "ok"
    assert get_schema_drift_report()["state"] == "ok"


def test_a_healthy_verdict_is_never_re_inspected(
    drifted_db, drifted_manager, monkeypatch
):
    """`ok` is final: nothing but create_all changes the schema under us, and it
    cannot remove a column. Re-inspecting would be a full pass per health scrape
    on every healthy deployment, which is the cost the cache exists to avoid."""
    monkeypatch.setattr(conn, "_SCHEMA_RECHECK_SECONDS", 0.0)
    with drifted_db.connect() as c:
        c.execute(text(f"ALTER TABLE {DRIFT_TABLE} ADD COLUMN {DRIFT_COLUMN} JSONB"))
        c.commit()

    manager = drifted_manager
    calls = []
    original = manager.schema_report
    _patch_report(monkeypatch, lambda: (calls.append(1), original())[1])

    for _ in range(3):
        assert check_schema_drift(db_manager=manager)["state"] == "ok"

    assert len(calls) == 1, f"expected one inspection, got {len(calls)}"


def test_an_empty_verdict_does_not_survive_a_create_all(drifted_manager, monkeypatch):
    """create_all runs on every DatabaseDataService construction, so a cached
    "empty" can be void by the next call — the tables may exist now.

    Without this, a process that first saw an unprovisioned database would keep
    refusing (strict mode) or keep reporting "empty" through the whole recheck
    window, even though it had just built the schema itself.
    """
    # Long enough that only the create_all rule can trigger a re-inspection.
    monkeypatch.setattr(conn, "_SCHEMA_RECHECK_SECONDS", 3600.0)
    manager = drifted_manager
    empty = {"state": "empty", "missing_tables": [], "missing_columns": {}}

    original = manager.schema_report
    _patch_report(monkeypatch, lambda: empty)
    # A caller that has not provisioned is entitled to an empty database.
    assert check_schema_drift(db_manager=manager, provisioned=False)["state"] == "empty"

    _patch_report(monkeypatch, original)
    assert (
        check_schema_drift(db_manager=manager, provisioned=True)["state"] == "drifted"
    )


def test_concurrent_callers_inspect_and_log_once(drifted_manager, caplog, monkeypatch):
    """init_database() is reached from request threads, not just startup.

    Two arriving together must not each walk every mapped table, nor each emit
    the same ERROR.
    """
    import threading

    calls = []
    original = drifted_manager.schema_report
    barrier = threading.Barrier(8)

    def slow_report():
        calls.append(1)
        time.sleep(0.05)
        return original()

    _patch_report(monkeypatch, slow_report)

    def worker():
        barrier.wait()
        check_schema_drift(db_manager=drifted_manager)

    with caplog.at_level(logging.ERROR):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(calls) == 1, f"expected one inspection, got {len(calls)}"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"expected a single ERROR, got {len(errors)}"


def test_strict_mode_is_not_swallowed_on_schema_drift(
    drifted_manager, monkeypatch, postgres_available
):
    """DatabaseDataService must not swallow an explicit schema-drift refusal.

    _init_database() catches Exception for a down database so reconnect can
    retry. SchemaDriftError is re-raised so DB_STRICT_SCHEMA=true stays fatal
    (#562).
    """
    from core.storage import database_data_service as dds

    _set_strict(monkeypatch, "true")
    monkeypatch.setattr(dds, "is_demo_mode", lambda: False, raising=True)
    monkeypatch.setattr(dds, "get_db_manager", lambda: drifted_manager, raising=True)
    monkeypatch.setattr(
        dds,
        "init_database",
        lambda **kw: check_schema_drift(db_manager=drifted_manager),
        raising=True,
    )

    with pytest.raises(SchemaDriftError):
        dds.DatabaseDataService()
