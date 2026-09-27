"""Database isolation for DB-backed unit tests (#747).

A unit test that writes to the database used to write to whatever the
environment pointed at, which for a developer is the database their own console
reads. ``TestApprovalQueue`` left three pending containment proposals behind per
run and 93 had piled up in the approvals queue before anyone connected the two.

Per-test cleanup would fix the tests that exist; provisioning a throwaway
database fixes the ones nobody has audited yet. Any unit test marked
``external_service`` is handed a fresh ``vigil_test_<pid>_<rand>`` built from
the ORM models, and the whole database is dropped afterwards, so a test that
forgets to tidy up cannot reach anyone's data.

Deliberately not a skip when the server is unreachable: the DB-backed CI job
exists to run these, and a fixture that skipped its way to green would retire
that gate silently. A missing Postgres is an error here.
"""

import os
import secrets

import pytest
from sqlalchemy import text

# `external_service` is the marker CI's DB-backed unit job selects on
# (`pytest tests/unit/ -m external_service`), so a test that needs PostgreSQL
# must already carry it to run in CI at all -- which makes it the one reliable
# signal. `database` deliberately does NOT appear here: it is used descriptively
# on offline tests too (tests/unit/storage/test_dsn_parsing.py parses strings
# and touches no server), and requiring a database for those breaks the no-
# service unit job, which runs `-m "not external_service"` with no Postgres.
_DB_MARKERS = ("external_service",)

# Mirrors what CI's "Enable Postgres extensions" step installs. Both ship with
# stock Postgres; pg_trgm is what the findings GIN index (gin_trgm_ops) needs.
# Individually tolerant, matching DatabaseManager.create_tables(): a missing
# extension should fail on the CREATE that needs it, naming the real problem,
# rather than here.
_EXTENSIONS = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
)


def _quote_ident(name: str) -> str:
    """Quote a database name for DDL, which cannot take a bind parameter."""
    return '"' + name.replace('"', '""') + '"'


def _run_autocommit(manager, *statements: str) -> None:
    """Execute DDL that cannot run inside a transaction, on the live engine.

    Uses the manager's own engine rather than a hand-built one so the platform
    DB proxy is honoured: DatabaseManager._build() rewrites host/port when
    proxy.enabled, and a raw create_engine() on config.host would miss it.
    """
    with manager.engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            conn.execute(text(statement))


@pytest.fixture(scope="session")
def throwaway_database():
    """Create a database for this pytest process, retarget at it, drop it after.

    Session-scoped: provisioning costs a `create_all`, and the tests that use it
    are already ordered arbitrarily, so per-test databases would buy isolation
    the residue problem does not need.
    """
    from core.storage.connection import DatabaseConfig, get_db_manager

    manager = get_db_manager()
    # Whatever the environment points at -- the developer's dev database, or the
    # test database CI provisions. Only ever the maintenance connection.
    origin = DatabaseConfig()
    if manager._engine is None:
        manager.initialize()

    # pid alone collides across PID namespaces: two containers sharing one
    # server can pick the same name, and the second run's DROP would take the
    # first run's live database with it.
    name = f"vigil_test_{os.getpid()}_{secrets.token_hex(4)}"
    ident = _quote_ident(name)
    _run_autocommit(manager, f"CREATE DATABASE {ident}")

    target = DatabaseConfig()
    target.database = name
    manager.retarget(target)

    # get_db_session() calls refresh_if_stale(), which re-reads DatabaseConfig()
    # when the secrets file mtime changes and would retarget silently back onto
    # the developer's database mid-session -- a running backend saving Settings
    # is enough to trigger it. A generation of 0.0 is the documented "no
    # propagation available" value and makes the check a no-op.
    import core.storage.connection as connection_module

    pinned = pytest.MonkeyPatch()
    pinned.setattr(connection_module, "db_config_generation", lambda: 0.0)

    try:
        for statement in _EXTENSIONS:
            try:
                _run_autocommit(manager, statement)
            except Exception as e:  # noqa: BLE001
                print(f"[test-db] {statement} failed, continuing: {e}")
        manager.create_tables()
        yield name
    finally:
        pinned.undo()
        # Point back at the origin first: that disposes the engine holding
        # connections to the database about to be dropped. Guarded so a failed
        # retarget cannot leak the database -- the drop has to run either way.
        try:
            manager.retarget(origin)
        finally:
            try:
                _run_autocommit(
                    manager, f"DROP DATABASE IF EXISTS {ident} WITH (FORCE)"
                )
            except Exception as e:  # noqa: BLE001
                # Say which database leaked rather than swallowing it: a
                # retarget that failed above leaves the engine connected to the
                # database being dropped, and Postgres refuses that.
                print(f"[test-db] could not drop {name}, clean it up by hand: {e}")


@pytest.fixture(autouse=True)
def _isolate_database(request):
    """Hand every DB-marked unit test the throwaway database.

    Autouse so it covers tests that have not been audited, but lazy: a test
    without a DB marker never requests the fixture, so the unit tests that need
    no database still run with no server present.
    """
    if any(request.node.get_closest_marker(m) for m in _DB_MARKERS):
        request.getfixturevalue("throwaway_database")


# Episodic rows, shared because two suites seed them: #732's read tests and
# #735's grant test. Every one of them reads the whole table, so ordering
# between tests would otherwise decide what they see.
@pytest.fixture
def episodic_session():
    from core.storage.connection import get_db_session
    from core.storage.models import (
        EpisodicDistilMarker,
        EpisodicGap,
        EpisodicReadLog,
        EpisodicSighting,
        EpisodicVerdict,
        EpisodicVerdictSource,
    )

    db = get_db_session()
    try:
        for model in (
            EpisodicVerdictSource,
            EpisodicVerdict,
            EpisodicSighting,
            EpisodicGap,
            EpisodicDistilMarker,
            EpisodicReadLog,
        ):
            db.query(model).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()
