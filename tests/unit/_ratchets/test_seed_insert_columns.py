"""A seed INSERT must name every NOT NULL column that Postgres cannot fill.

compose's db-seed and ``scripts/seed_reference_data.py`` apply
``infra/database/init/*.sql`` with raw SQL after ``create_all``, and the backend
applies the INSERTs of ``05_case_management_extended.sql`` the same way
(``core/storage/reference_seed.py``). A column the models give only a
Python-side ``default=`` has no DEFAULT in the DDL ``create_all`` emits, so an
INSERT that leaves it out writes NULL and fails. Every template in 05 did that
with ``case_templates.usage_count``. The appliers tolerated the failures, so the
default templates were never seeded and nothing said so.
"""

from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table

from core.storage.models import Base
from core.storage.reference_seed import INSERT_TARGET, split_statements, target_table

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = REPO / "infra" / "database" / "init"


def _inserts(sql: str):
    """(table, named columns or None) for every INSERT the seeders would run.

    Split the way the seeder script and the backend split, so the check sees
    the statements they execute.
    """
    for statement in split_statements(sql):
        for match in INSERT_TARGET.finditer(statement):
            table = target_table(match)
            columns = match.group(2)
            if columns is not None:
                columns = {c.strip().strip('"') for c in columns.split(",")}
            yield table, columns


def _filled_by_postgres(column) -> bool:
    return (
        column.server_default is not None
        or column.identity is not None
        or column.computed is not None
        or column is column.table.autoincrement_column
    )


def _unfilled(metadata: MetaData, sql: str) -> list:
    problems = []
    for name, named in _inserts(sql):
        table = metadata.tables.get(name)
        if table is None:
            # Not a model table: the SQL that creates it sets its defaults.
            continue
        if named is None:
            problems.append(f"INSERT INTO {name} names no columns")
            continue
        missing = sorted(
            c.name
            for c in table.columns
            if not c.nullable and c.name not in named and not _filled_by_postgres(c)
        )
        if missing:
            problems.append(f"INSERT INTO {name} omits {', '.join(missing)}")
    return problems


def test_the_check_catches_a_python_only_default():
    # Without this, a check that matched nothing would pass the test below.
    metadata = MetaData()
    Table(
        "t",
        metadata,
        Column("id", String(10), primary_key=True),
        Column("uses", Integer, nullable=False, default=0),
        Column("seen", Integer, nullable=False, default=0, server_default="0"),
    )
    assert _unfilled(metadata, "INSERT INTO t (id) VALUES ('a');") == [
        "INSERT INTO t omits uses"
    ]
    assert _unfilled(metadata, "INSERT INTO t (id, uses) VALUES ('a', 0);") == []
    assert _unfilled(metadata, "INSERT INTO t VALUES ('a', 0, 0);") == [
        "INSERT INTO t names no columns"
    ]


def test_the_scan_reaches_the_case_management_seed():
    seed = (INIT_SQL / "05_case_management_extended.sql").read_text(encoding="utf-8")
    assert {"sla_policies", "case_templates"} <= {t for t, _ in _inserts(seed)}


def test_every_seed_insert_names_the_columns_postgres_cannot_fill():
    problems = [
        f"{path.name}: {problem}"
        for path in sorted(INIT_SQL.glob("*.sql"))
        for problem in _unfilled(Base.metadata, path.read_text(encoding="utf-8"))
    ]
    assert not problems, (
        "create_all gives a Python-side default= no DEFAULT, so these INSERTs "
        "write NULL into a NOT NULL column and fail. Name the column in the "
        "INSERT, or give the model a server_default.\n" + "\n".join(problems)
    )
