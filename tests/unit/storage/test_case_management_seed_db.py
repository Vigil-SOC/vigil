"""The case-management seed on the tables create_all builds.

compose's db-seed and ``scripts/seed_reference_data.py`` apply
``05_case_management_extended.sql`` after create_all, one statement at a time,
and tolerate every failure. A statement Postgres rejects leaves nothing behind
but a missing row, which is how the eight default case templates went missing:
``case_templates.usage_count`` was NOT NULL with no DEFAULT and the seed left it
out. Each test runs in a transaction that is rolled back, with a savepoint per
statement, so the shared database is left as it was found.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError

from core.storage.connection import get_db_manager
from core.storage.reference_seed import split_statements

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "infra" / "database" / "init" / "05_case_management_extended.sql"
SEED_SQL = SEED.read_text(encoding="utf-8")
POLICY_IDS = sorted(set(re.findall(r"'(sla-[a-z]+-default)'", SEED_SQL)))
TEMPLATE_IDS = sorted(set(re.findall(r"'(template-[a-z-]+-\d+)'", SEED_SQL)))

SEEDED_TEMPLATES = text(
    "SELECT count(*), coalesce(max(usage_count), -1) FROM case_templates "
    "WHERE template_id IN :ids"
).bindparams(bindparam("ids", expanding=True))
SEEDED_POLICIES = text(
    "SELECT count(*) FROM sla_policies WHERE policy_id IN :ids"
).bindparams(bindparam("ids", expanding=True))
DROP_DEFAULT = text("ALTER TABLE case_templates ALTER COLUMN usage_count DROP DEFAULT")


def _apply(conn, sql: str) -> list:
    """Run each statement as the seeder does; return the ones Postgres refused."""
    refused = []
    for statement in split_statements(sql):
        savepoint = conn.begin_nested()
        try:
            conn.execute(text(statement))
            savepoint.commit()
        except DBAPIError as e:
            savepoint.rollback()
            refused.append(str(e.orig).splitlines()[0])
    return refused


def _assert_seeded(conn):
    assert conn.execute(SEEDED_POLICIES, {"ids": POLICY_IDS}).scalar_one() == len(
        POLICY_IDS
    )
    assert tuple(conn.execute(SEEDED_TEMPLATES, {"ids": TEMPLATE_IDS}).one()) == (
        len(TEMPLATE_IDS),
        0,
    )


def test_the_seed_ids_were_found():
    # Without these the assertions below would compare empty sets.
    assert POLICY_IDS and TEMPLATE_IDS


def test_seed_applies_after_create_all():
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            assert conn.execute(SEEDED_TEMPLATES, {"ids": TEMPLATE_IDS}).one()[0] == 0
            assert _apply(conn, SEED_SQL) == []
            _assert_seeded(conn)
        finally:
            tx.rollback()


def test_seed_applies_to_a_table_built_before_the_default():
    # An existing database keeps the table create_all built without a DEFAULT;
    # the seed names usage_count, so it does not depend on one.
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(DROP_DEFAULT)
            assert _apply(conn, SEED_SQL) == []
            _assert_seeded(conn)
        finally:
            tx.rollback()

