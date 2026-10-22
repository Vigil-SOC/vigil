"""The finding-side index on case_findings, in a real Postgres.

The primary key is (case_id, finding_id), so it cannot serve a lookup by
finding. Here the subject is what create_all builds and that the planner takes
the index for that lookup. Each change runs inside a transaction that is rolled
back, so the shared database is left as found.
"""

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

INDEX = "idx_case_findings_finding"


def _index_def(conn):
    return conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = :name"
        ),
        {"name": INDEX},
    ).scalar_one_or_none()


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

