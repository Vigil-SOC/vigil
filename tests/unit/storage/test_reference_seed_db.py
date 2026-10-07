"""The backend seeds the default SLA policies and case templates into empty tables.

A Helm install never ran the SQL seed after create_all, so without this it had
neither. tests/unit/conftest.py keeps the shared test database unseeded, which
is what lets these tests start from empty tables; each one rolls back or
deletes what it wrote, so the database is left as it was found.
"""

import logging
import re

import pytest
from sqlalchemy import text

import core.storage.connection as connection_module
from core.storage.connection import (
    get_db_manager,
    init_database,
    reset_reference_seed_check,
    seed_reference_tables,
)
from core.storage.reference_seed import find_seed_file, seed_empty_tables

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

SEED_SQL = find_seed_file().read_text(encoding="utf-8")
EXPECTED = {
    table: len(re.findall(rf"INSERT INTO {table}\b", SEED_SQL))
    for table in ("sla_policies", "case_templates")
}
LOGGER = connection_module.logger.name

OWN_TEMPLATE = text(
    "INSERT INTO case_templates (template_id, name, template_type, "
    "default_priority, default_status, task_templates, is_active) VALUES "
    "('template-own-001', 'Own', 'custom', 'low', 'open', '[]'::jsonb, true)"
)


def _count(conn, table: str) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


@pytest.fixture
def conn():
    with get_db_manager().engine.connect() as connection:
        tx = connection.begin()
        try:
            yield connection
        finally:
            tx.rollback()


@pytest.fixture
def fresh_check():
    reset_reference_seed_check()
    yield
    reset_reference_seed_check()


def test_the_seed_names_both_tables():
    # Without this the assertions below could compare empty dicts.
    assert all(EXPECTED.values())


def test_empty_tables_get_every_default_row(conn):
    assert _count(conn, "sla_policies") == 0
    assert _count(conn, "case_templates") == 0

    assert seed_empty_tables(conn) == (EXPECTED, {})
    assert _count(conn, "sla_policies") == EXPECTED["sla_policies"]
    assert _count(conn, "case_templates") == EXPECTED["case_templates"]
    usage = conn.execute(text("SELECT max(usage_count) FROM case_templates"))
    assert usage.scalar_one() == 0


def test_a_table_with_rows_is_left_alone(conn):
    # The operator's own template, or the defaults less one they deleted: the
    # table is theirs, and a restart must not put a default back.
    conn.execute(OWN_TEMPLATE)

    inserted, failed = seed_empty_tables(conn)
    assert failed == {}
    assert inserted == {"sla_policies": EXPECTED["sla_policies"]}
    assert _count(conn, "case_templates") == 1

    assert seed_empty_tables(conn) == ({}, {})


def test_a_failing_table_leaves_the_others_seeded(conn):
    sql = (
        "INSERT INTO sla_policies (policy_id, name, priority_level, "
        "response_time_hours, resolution_time_hours, business_hours_only, "
        "is_active, is_default) "
        "VALUES ('sla-seed-test', 'Seed test', 'low', 1.0, 2.0, false, true, false);\n"
        "INSERT INTO case_templates (template_id) VALUES ('template-seed-test');\n"
    )
    inserted, failed = seed_empty_tables(conn, sql)
    assert inserted == {"sla_policies": 1}
    assert list(failed) == ["case_templates"]
    assert "null value" in failed["case_templates"]
    assert _count(conn, "sla_policies") == 1
    assert _count(conn, "case_templates") == 0


def test_init_database_seeds_through_the_real_seed(monkeypatch, fresh_check):
    # The conftest keeps init_database() from seeding the shared database;
    # restore the real seed for this one test and delete what it wrote.
    monkeypatch.setattr(connection_module, "seed_empty_tables", seed_empty_tables)
    engine = get_db_manager().engine
    with engine.connect() as c:
        assert _count(c, "sla_policies") == 0 and _count(c, "case_templates") == 0
    try:
        init_database()
        with engine.connect() as c:
            assert _count(c, "sla_policies") == EXPECTED["sla_policies"]
            assert _count(c, "case_templates") == EXPECTED["case_templates"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM case_templates"))
            c.execute(text("DELETE FROM sla_policies"))


def test_each_database_is_seeded_once_per_process(monkeypatch, fresh_check):
    # init_database() runs on every DatabaseDataService construction.
    calls = []
    monkeypatch.setattr(
        connection_module,
        "seed_empty_tables",
        lambda conn, sql=None: calls.append(sql) or ({}, {}),
    )
    init_database()
    init_database()
    init_database(create_tables=False)
    assert len(calls) == 1


def test_a_failure_is_logged_and_retried_after_the_recheck_interval(
    monkeypatch, caplog, fresh_check
):
    results = iter([({}, {"case_templates": "boom"}), ({"case_templates": 8}, {})])
    calls = []

    def fake(conn, sql=None):
        calls.append(sql)
        return next(results)

    monkeypatch.setattr(connection_module, "seed_empty_tables", fake)
    monkeypatch.setattr(connection_module, "_SCHEMA_RECHECK_SECONDS", 3600.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        seed_reference_tables()
        seed_reference_tables()
        assert len(calls) == 1, "retried inside the recheck interval"
        assert "Could not seed the default rows of case_templates: boom" in caplog.text

        monkeypatch.setattr(connection_module, "_SCHEMA_RECHECK_SECONDS", 0.0)
        seed_reference_tables()
        seed_reference_tables()
    assert len(calls) == 2, "not retried after the interval, or retried after success"
    assert "Seeded 8 default row(s) into the empty table case_templates" in caplog.text


def test_a_missing_seed_file_is_reported_once(monkeypatch, caplog, fresh_check):
    calls = []

    def missing(conn, sql=None):
        calls.append(sql)
        raise FileNotFoundError("05_case_management_extended.sql is not under /app")

    monkeypatch.setattr(connection_module, "seed_empty_tables", missing)
    monkeypatch.setattr(connection_module, "_SCHEMA_RECHECK_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        seed_reference_tables()
        seed_reference_tables()
    assert len(calls) == 1
    assert "not seeded: 05_case_management_extended.sql" in caplog.text
