"""The now() default in a real Postgres: what create_all builds.

``test_now_server_default.py`` checks the compiled DDL. Here the subject is the
database this job provisions with create_all.
"""

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# How a frozen default reads back: '2026-09-28 15:52:47.092872'::timestamp ...
FROZEN = text("""
    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = current_schema() AND column_default LIKE '''%''::timestamp%'
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


def test_create_all_builds_no_frozen_default():
    with get_db_manager().engine.connect() as conn:
        assert conn.execute(FROZEN).all() == []
        assert _default(conn, "sla_policies", "created_at") == "now()"

