"""The now() default in a real Postgres: what create_all builds, and the repair.

``test_now_server_default.py`` checks the compiled DDL. Here the subject is the
database this job provisions with create_all, and the repair in
``scripts/migrate_schema.py`` for databases built before the models were fixed.
The repair runs inside a transaction that is rolled back, so the shared
database is left as it was found.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

MIGRATE_SCHEMA = Path(__file__).resolve().parents[3] / "scripts" / "migrate_schema.py"

# How a frozen default reads back: '2026-09-28 15:52:47.092872'::timestamp ...
FROZEN = text("""
    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = current_schema() AND column_default LIKE '''%''::timestamp%'
    """)

# A raw-SQL INSERT that names no timestamp, so it takes the table's defaults.
SEED_POLICY = text("""
    INSERT INTO sla_policies (
        policy_id, name, description, priority_level,
        response_time_hours, resolution_time_hours,
        business_hours_only, notification_thresholds,
        is_active, is_default
    ) VALUES (
        'sla-now-default-test', 'Now default test', 'rolled back', 'low',
        1.0, 2.0, false, ARRAY[75, 90, 100], true, false
    )
    RETURNING created_at = localtimestamp
    """)


def _default(conn, table: str, column: str) -> str:
    return conn.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).scalar_one()


def _repair():
    # scripts/ is not a package; the migrator is a CLI file, so load it by path.
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fix_frozen_now_defaults


def test_create_all_builds_no_frozen_default():
    with get_db_manager().engine.connect() as conn:
        assert conn.execute(FROZEN).all() == []
        assert _default(conn, "sla_policies", "created_at") == "now()"


def test_migration_resets_frozen_and_missing_now_defaults():
    repair = _repair()
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            # A table created long ago by the old models, and one whose column
            # never had a default. "timestamp" also needs quoting.
            conn.execute(
                text(
                    "ALTER TABLE sla_policies "
                    "ALTER COLUMN created_at SET DEFAULT '2026-01-01 00:00:00'"
                )
            )
            conn.execute(
                text(
                    'ALTER TABLE case_audit_logs ALTER COLUMN "timestamp" DROP DEFAULT'
                )
            )
            assert conn.execute(SEED_POLICY).scalar_one() is False

            assert repair(conn) == [
                ("case_audit_logs", "timestamp"),
                ("sla_policies", "created_at"),
            ]
            assert _default(conn, "sla_policies", "created_at") == "now()"
            assert _default(conn, "case_audit_logs", "timestamp") == "now()"
            assert conn.execute(FROZEN).all() == []

            conn.execute(
                text(
                    "DELETE FROM sla_policies WHERE policy_id = 'sla-now-default-test'"
                )
            )
            assert conn.execute(SEED_POLICY).scalar_one() is True

            assert repair(conn) == []
        finally:
            tx.rollback()
