"""findings.bulk_imported reaches existing databases, and marks what they hold.

Rows stored before the column read NULL; the unrated ones are marked by the old
guess (no event time, or stored over 24h after it), the rated ones left alone.
migrate_schema.py and 37_findings_bulk_imported.sql must agree.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager
from core.storage.models import Finding

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
MIGRATE_SCHEMA = REPO / "scripts" / "migrate_schema.py"
INIT_SQL = REPO / "infra" / "database" / "init" / "37_findings_bulk_imported.sql"


@functools.cache
def _migrate():
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _autocommit():
    return (
        get_db_manager()
        .engine.connect()
        .execution_options(isolation_level="AUTOCOMMIT")
    )


def _run_migrate_steps():
    migrate = _migrate()
    with get_db_manager().engine.begin() as conn:
        migrate.add_findings_bulk_imported(conn)
    with _autocommit() as conn:
        migrate.create_findings_unrated_index(conn)
    with get_db_manager().engine.begin() as conn:
        migrate.mark_findings_bulk_imported(conn)


def _statements(sql):
    # One at a time, as psql -f sends them; DO bodies hold their own semicolons.
    statements, lines, in_body = [], [], False
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        lines.append(line)
        in_body ^= line.count("$$") % 2 == 1
        if not in_body and line.rstrip().endswith(";"):
            statements.append("\n".join(lines))
            lines = []
    return statements


def _apply_init_sql():
    with _autocommit() as conn:
        for statement in _statements(INIT_SQL.read_text(encoding="utf-8")):
            conn.exec_driver_sql(statement)


def _store_without_the_column():
    with _autocommit() as conn:
        conn.execute(text("ALTER TABLE findings DROP COLUMN IF EXISTS bulk_imported"))
        for finding_id, enrichment, event_ago in (
            ("bfmig-old-event", None, "30 days"),
            ("bfmig-no-event", None, None),
            ("bfmig-on-time", None, "1 minute"),
            ("bfmig-failed-old", '{"ai_triage_error": "down"}', "30 days"),
            ("bfmig-rated-old", '{"ai_triage": {}}', "30 days"),
        ):
            conn.execute(
                text(
                    "INSERT INTO findings (finding_id, data_source, ai_enrichment, "
                    "created_at, timestamp) VALUES (:id, 'bfmig', "
                    "CAST(:enrichment AS jsonb), now() AT TIME ZONE 'utc', "
                    "now() AT TIME ZONE 'utc' - CAST(:event_ago AS interval))"
                ),
                {"id": finding_id, "enrichment": enrichment, "event_ago": event_ago},
            )


def _marks():
    with _autocommit() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT finding_id, bulk_imported FROM findings "
                    "WHERE finding_id LIKE 'bfmig-%'"
                )
            ).all()
        )


@pytest.fixture(autouse=True)
def restored_schema():
    yield
    # The rest of the session expects the model's column and index.
    with _autocommit() as conn:
        conn.execute(text("DELETE FROM findings WHERE finding_id LIKE 'bfmig-%'"))
    _run_migrate_steps()
    with _autocommit() as conn:
        conn.execute(
            text(
                "UPDATE findings SET bulk_imported = false WHERE bulk_imported IS NULL"
            )
        )


@pytest.mark.parametrize("apply", [_run_migrate_steps, _apply_init_sql])
def test_marks_unrated_rows_stored_before_the_column(apply):
    _store_without_the_column()

    apply()

    assert _marks() == {
        "bfmig-old-event": True,
        "bfmig-no-event": True,
        "bfmig-on-time": False,
        "bfmig-failed-old": True,
        "bfmig-rated-old": None,
    }
    with _autocommit() as conn:
        valid = conn.execute(
            text(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = to_regclass('idx_finding_unrated_sweep')"
            )
        ).scalar()
    assert valid is True


@pytest.mark.parametrize("apply", [_run_migrate_steps, _apply_init_sql])
def test_a_second_run_changes_nothing_and_new_rows_default_live(apply):
    _store_without_the_column()
    apply()
    with _autocommit() as conn:
        conn.execute(
            text(
                "INSERT INTO findings (finding_id, data_source, timestamp) "
                "VALUES ('bfmig-new', 'bfmig', NULL)"
            )
        )

    apply()

    assert _marks()["bfmig-new"] is False
    assert _marks()["bfmig-rated-old"] is None


def test_the_runner_adds_indexes_and_marks_in_order(monkeypatch):
    migrate = _migrate()
    steps = [
        ("column", migrate.add_findings_bulk_imported),
        ("index", migrate.create_findings_unrated_index),
        ("mark", migrate.mark_findings_bulk_imported),
    ]
    monkeypatch.setattr(migrate, "MIGRATIONS", steps)
    _store_without_the_column()
    url = get_db_manager().engine.url.render_as_string(hide_password=False)

    result = migrate.run_migrations(url)

    assert result["failed"] == [] and len(result["applied"]) == 3
    assert _marks()["bfmig-old-event"] is True


def test_bulk_create_marks_imports_and_create_finding_does_not():
    from core.storage.service import DatabaseService

    service = DatabaseService()
    service.bulk_create_findings([{"finding_id": "bfmig-bulk", "data_source": "bfmig"}])
    service.create_finding("bfmig-live", {}, None, None, "bfmig")

    with get_db_manager().session_scope() as session:
        marks = dict(
            session.query(Finding.finding_id, Finding.bulk_imported).filter(
                Finding.finding_id.in_(["bfmig-bulk", "bfmig-live"])
            )
        )

    assert marks == {"bfmig-bulk": True, "bfmig-live": False}
