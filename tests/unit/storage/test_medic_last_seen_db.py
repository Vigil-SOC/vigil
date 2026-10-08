"""``medic_last_seen`` in a real Postgres: create_all, the init SQL, the migration.

A fresh install gets the table from create_all; a database built before it gets
it from ``41_medic_last_seen.sql`` (Compose db-seed, Helm db-init) or from
``scripts/migrate_schema.py``. All three must build the same table, with the
same limits: one row, a closed ``failure_kind``, a size-capped snapshot. Each
change runs in a transaction that is rolled back, so the shared test database
is left as found.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
MIGRATE_SCHEMA = REPO / "scripts" / "migrate_schema.py"
INIT_SQL = REPO / "infra" / "database" / "init" / "41_medic_last_seen.sql"
HELM_SQL = REPO / "infra" / "helm" / "vigil" / "files" / "database-init" / INIT_SQL.name

COLUMNS = {
    "id": ("smallint", "NO"),
    "last_seen_at": ("timestamp without time zone", "YES"),
    "first_failed_at": ("timestamp without time zone", "YES"),
    "failure_kind": ("character varying", "YES"),
    "status_snapshot": ("jsonb", "YES"),
    "updated_at": ("timestamp without time zone", "NO"),
}


def _columns(conn):
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'medic_last_seen'"
        )
    ).all()
    return {name: (kind, nullable) for name, kind, nullable in rows}


def _checks(conn):
    rows = conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'medic_last_seen'::regclass AND contype = 'c'"
        )
    ).all()
    return dict(rows)


def _step():
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.add_medic_last_seen


def _insert(conn, **values):
    row = {"id": 1, "updated_at": "2026-10-08 12:00:00", **values}
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    conn.execute(text(f"INSERT INTO medic_last_seen ({cols}) VALUES ({params})"), row)


@pytest.fixture
def conn():
    engine = get_db_manager()._engine
    with engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


def test_create_all_builds_the_table(conn):
    assert _columns(conn) == COLUMNS
    assert set(_checks(conn)) == {
        "medic_last_seen_one_row",
        "medic_last_seen_failure_kind",
        "medic_last_seen_snapshot_typed",
    }


@pytest.mark.parametrize("how", ["init_sql", "migration"])
def test_init_sql_and_migration_build_the_same_table(conn, how):
    orm_checks = _checks(conn)
    conn.execute(text("DROP TABLE medic_last_seen"))
    if how == "init_sql":
        conn.execute(text(INIT_SQL.read_text()))
        conn.execute(text(INIT_SQL.read_text()))  # db-seed re-applies it
    else:
        _step()(conn)
        _step()(conn)
    assert _columns(conn) == COLUMNS
    assert _checks(conn) == orm_checks
    _insert(conn, failure_kind="refused")


def test_the_migration_leaves_an_existing_row(conn):
    _insert(conn, failure_kind="timeout")
    _step()(conn)
    assert (
        conn.execute(text("SELECT failure_kind FROM medic_last_seen")).scalar()
        == "timeout"
    )


def test_no_row_on_a_fresh_install(conn):
    assert conn.execute(text("SELECT count(*) FROM medic_last_seen")).scalar() == 0


def test_the_helm_copy_matches():
    assert HELM_SQL.read_text() == INIT_SQL.read_text()


@pytest.mark.parametrize(
    "values",
    [
        {"id": 2},
        {"failure_kind": "down"},
        {"failure_kind": "404"},
        {"status_snapshot": '{"x": "' + "a" * 5000 + '"}'},
        {"status_snapshot": '"a string"'},
    ],
)
def test_the_limits_hold_in_the_database(conn, values):
    nested = conn.begin_nested()
    with pytest.raises(IntegrityError):
        _insert(conn, **values)
    nested.rollback()


def test_only_one_row(conn):
    _insert(conn)
    nested = conn.begin_nested()
    with pytest.raises(IntegrityError):
        _insert(conn)
    nested.rollback()
