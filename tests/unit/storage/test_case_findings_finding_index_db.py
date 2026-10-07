"""The finding-side index on case_findings, in a real Postgres.

The primary key is (case_id, finding_id), so it cannot serve a lookup by
finding. Here the subject is what create_all builds, that the planner takes
the index for that lookup, and the step in ``scripts/migrate_schema.py`` that
adds it to a table built before the model declared it. Each change runs inside
a transaction that is rolled back, so the shared database is left as found.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

MIGRATE_SCHEMA = Path(__file__).resolve().parents[3] / "scripts" / "migrate_schema.py"

INDEX = "idx_case_findings_finding"


def _index_def(conn):
    return conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :name"
        ),
        {"name": INDEX},
    ).scalar_one_or_none()


def _step():
    # scripts/ is not a package; the migrator is a CLI file, so load it by path.
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_case_findings_finding_index


def test_create_all_builds_the_finding_index():
    with get_db_manager().engine.connect() as conn:
        assert (_index_def(conn) or "").endswith(
            "case_findings USING btree (finding_id, case_id)"
        )


def test_a_lookup_by_finding_uses_the_index():
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            # The test table is near empty, where a sequential scan always
            # wins; forbid it to see which index the planner would take.
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(
                conn.execute(
                    text(
                        "EXPLAIN SELECT case_id FROM case_findings "
                        "WHERE finding_id = 'f-1'"
                    )
                ).scalars()
            )
            assert INDEX in plan, plan
        finally:
            tx.rollback()


def test_migration_adds_the_index_once():
    step = _step()
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text(f"DROP INDEX {INDEX}"))
            assert _index_def(conn) is None

            step(conn)
            assert "(finding_id, case_id)" in _index_def(conn)

            # A second run finds the index and leaves it.
            step(conn)
            assert "(finding_id, case_id)" in _index_def(conn)
        finally:
            tx.rollback()
