"""Schema drift on ORM-only tables — the upgrade path, not the fresh-build path.

CI only ever builds from an empty database, which is exactly why #562 was
invisible: `create_all` produces a complete schema from scratch, so a column
added to a model looks fine in CI and is missing on every existing deployment.

These tests provision a database, move the *model* forward relative to it, and
assert on what an upgrade actually does. Three current behaviours are pinned as
characterisation tests — `create_all` will not alter an existing table, the
detector notices, the ORM then raises — so that a regression in any of them is
visible. The rest cover the startup check added for #562.
"""

import logging
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

from database.connection import (
    DatabaseManager,
    SchemaDriftError,
    check_schema_drift,
    get_schema_drift_report,
    reset_schema_drift_check,
)
from database.models import Base, CaseWatcher

pytestmark = [pytest.mark.integration, pytest.mark.database]

# An ORM-only table: no CREATE TABLE for it exists anywhere in database/init/
# or the Helm bundle, so create_all is the only thing that can build it and the
# numbered-init-file mechanism cannot reach it at all.
DRIFT_TABLE = "case_watchers"
DRIFT_COLUMN = "notification_preferences"

SCRATCH_DB = "vigil_test_schema_drift"


def _url(database: str) -> str:
    """Build a DSN from the POSTGRES_* variables CI sets, else dev defaults.

    CI's integration job uses test/test/deeptempo_test; a developer's compose
    stack uses deeptempo/deeptempo_secure_password_change_me. Hardcoding either
    makes the test silently skip in the other environment.
    """
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


# 'postgres' always exists and is never the target, so it is safe to CREATE and
# DROP the scratch database from.
ADMIN_URL = _url("postgres")
SCRATCH_URL = _url(SCRATCH_DB)


def _postgres_available() -> bool:
    try:
        eng = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        with eng.connect():
            return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _postgres_available(),
        reason="requires a local PostgreSQL (docker compose up -d postgres)",
    )
)


@pytest.fixture
def drifted_db():
    """A database provisioned at 'version N', then rewound behind the model.

    Dropping the column is how we emulate a database that predates it: the
    deployed schema is one release behind what models.py now declares. This is
    the situation every existing deployment is in after a column is added.
    """
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(SCRATCH_URL)
    with scratch.connect() as c:
        # create_all cannot provision from a bare database on its own: the
        # findings GIN index needs pg_trgm or it fails with
        # 'operator class gin_trgm_ops does not exist'.
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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


@pytest.fixture
def drifted_manager(drifted_db):
    """A DatabaseManager pointed at the drifted database.

    `_engine` is assigned directly on purpose: DatabaseManager resolves its URL
    through the config layer (database/connection.py:403) and ignores the
    DATABASE_URL environment variable, so setting that env var would silently
    inspect the developer's real database and report a healthy schema.
    """
    dm = DatabaseManager()
    dm._engine = drifted_db
    return dm


@pytest.fixture(autouse=True)
def _reset_drift_state(monkeypatch):
    """The startup check memoises; each test needs a clean slate."""
    monkeypatch.delenv("DB_STRICT_SCHEMA", raising=False)
    reset_schema_drift_check()
    yield
    reset_schema_drift_check()


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
    monkeypatch.setenv("DB_STRICT_SCHEMA", "true")

    with pytest.raises(SchemaDriftError) as excinfo:
        check_schema_drift(db_manager=drifted_manager)

    assert f"{DRIFT_TABLE}.{DRIFT_COLUMN}" in str(excinfo.value)


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "on"])
def test_strict_mode_accepts_common_truthy_spellings(
    drifted_manager, monkeypatch, value
):
    monkeypatch.setenv("DB_STRICT_SCHEMA", value)
    with pytest.raises(SchemaDriftError):
        check_schema_drift(db_manager=drifted_manager)


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_strict_mode_off_for_falsey_spellings(drifted_manager, monkeypatch, value):
    monkeypatch.setenv("DB_STRICT_SCHEMA", value)
    check_schema_drift(db_manager=drifted_manager)  # must not raise


def test_healthy_schema_logs_no_error(drifted_db, caplog):
    """No false alarms: a correct schema must stay quiet."""
    with drifted_db.connect() as c:
        c.execute(text(f"ALTER TABLE {DRIFT_TABLE} ADD COLUMN {DRIFT_COLUMN} JSONB"))
        c.commit()

    dm = DatabaseManager()
    dm._engine = drifted_db

    with caplog.at_level(logging.ERROR):
        report = check_schema_drift(db_manager=dm)

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


def test_check_runs_once_and_is_memoised(drifted_manager):
    """init_database() is called on every DatabaseDataService construction —
    including from the health endpoint — so an unmemoised check would add a
    full inspector pass per request."""
    calls = []
    original = drifted_manager.schema_report

    def counting_report():
        calls.append(1)
        return original()

    drifted_manager.schema_report = counting_report

    check_schema_drift(db_manager=drifted_manager)
    check_schema_drift(db_manager=drifted_manager)
    check_schema_drift(db_manager=drifted_manager)

    assert len(calls) == 1, f"expected one inspection, got {len(calls)}"
