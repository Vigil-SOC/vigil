"""migrate_schema.py keeps idx_finding_unrated_sweep one the planner reads.

It builds the index concurrently, leaves a working one alone, and rebuilds one
that a failed build left INVALID or whose WHERE no longer matches UNRATED_WHERE.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.storage.connection import get_db_manager
from core.storage.models import Finding
from core.storage.models.finding import UNRATED_WHERE

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

MIGRATE_SCHEMA = Path(__file__).resolve().parents[3] / "scripts" / "migrate_schema.py"
INDEX = "idx_finding_unrated_sweep"
PROBE = (
    f"SELECT 1 FROM findings WHERE {UNRATED_WHERE} "
    "ORDER BY bulk_imported, created_at LIMIT 1"
)


@functools.cache
def _migrate():
    # scripts/ is not a package; the migrator is a CLI file, so load it by path.
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


def _run_step():
    with _autocommit() as conn:
        _migrate().create_findings_unrated_index(conn)


def _index():
    with _autocommit() as conn:
        return conn.execute(
            text(
                "SELECT indexrelid AS oid, indisvalid AS valid, "
                "indisunique AS is_unique, "
                "pg_get_expr(indpred, indrelid) AS predicate "
                "FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ),
            {"name": INDEX},
        ).one_or_none()


def _replace_index(ddl=None):
    with _autocommit() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
        if ddl:
            conn.execute(text(ddl))


def _planner_reads_it():
    with _autocommit() as conn:
        return _migrate()._planner_reads(conn, INDEX, PROBE)


@pytest.fixture(autouse=True)
def restored_index():
    yield
    # The rest of the session expects the model's index, valid.
    with get_db_manager().session_scope() as session:
        session.query(Finding).filter(Finding.finding_id.like("idxmig-%")).delete(
            synchronize_session=False
        )
    _replace_index()
    _run_step()


def test_builds_the_index_where_there_is_none():
    _replace_index()

    _run_step()

    built = _index()
    assert built.valid
    assert "ai_triage_error" in built.predicate
    assert _planner_reads_it()


def test_leaves_a_working_index_alone():
    before = _index()
    assert before.valid

    _run_step()

    assert _index().oid == before.oid


def test_rebuilds_an_index_whose_where_has_moved_on():
    _replace_index(
        f"CREATE INDEX {INDEX} ON findings (bulk_imported, created_at) "
        "WHERE ai_enrichment IS NULL"
    )
    stale = _index()
    assert not _planner_reads_it()

    _run_step()

    rebuilt = _index()
    assert rebuilt.oid != stale.oid
    assert "ai_triage_error" in rebuilt.predicate
    assert _planner_reads_it()


def test_replaces_the_invalid_index_a_failed_build_leaves():
    _replace_index()
    with get_db_manager().session_scope() as session:
        for n in (1, 2):
            session.add(Finding(finding_id=f"idxmig-{n}", data_source="idxmig"))
    # A concurrent build that fails part-way keeps its name, marked INVALID.
    with _autocommit() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX} "
                f"ON findings (data_source) WHERE {UNRATED_WHERE}"
            )
        )
    left = _index()
    assert left is not None and not left.valid

    _run_step()

    rebuilt = _index()
    assert rebuilt.valid and not rebuilt.is_unique
    assert rebuilt.oid != left.oid


def test_the_runner_builds_it_outside_a_transaction(monkeypatch):
    migrate = _migrate()
    step = migrate.create_findings_unrated_index
    monkeypatch.setattr(migrate, "MIGRATIONS", [("unrated index", step)])
    _replace_index()
    url = get_db_manager().engine.url.render_as_string(hide_password=False)

    result = migrate.run_migrations(url)

    assert (result["applied"], result["failed"]) == ([(1, "unrated index")], [])
    assert _index().valid
