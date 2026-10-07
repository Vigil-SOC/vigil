"""An index a model declares on a table the init SQL creates must come from the SQL.

create_all is checkfirst=True: it builds a missing table with every index its
model declares, and it never adds an index to a table it finds. Compose and
Helm create some tables from infra/database/init/ first, so an index declared
only on the model never reaches those installs. idx_workflow_runs_triggered_by
was one: 12_workflow_runs.sql created workflow_runs without it, and only
scripts/migrate_schema.py, run as the table's owner, added it.

A model index counts as created when the init SQL creates an index of the same
name, or one on the same table over the same columns in the same order.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.storage.models import Base

pytestmark = pytest.mark.unit

INIT_SQL = Path(__file__).resolve().parents[3] / "infra" / "database" / "init"

COMMENT = re.compile(r"--[^\n]*")
CREATE_TABLE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\(", re.I)
CREATE_INDEX = re.compile(
    r"CREATE (?:UNIQUE )?INDEX (?:CONCURRENTLY )?(?:IF NOT EXISTS )?(\w+)\s+"
    r"ON (?:ONLY )?(\w+)\s*(?:USING \w+\s*)?\((.*?)\)\s*(?:WHERE\b|;)",
    re.I | re.S,
)


def _columns(listing: str) -> tuple[str, ...]:
    # "workflow_id, started_at DESC" and "description gin_trgm_ops" name their
    # columns first.
    return tuple(
        part.split()[0].strip('"').lower()
        for part in listing.split(",")
        if part.strip()
    )


def _init_sql():
    tables, indexes = set(), []
    for path in sorted(INIT_SQL.glob("*.sql")):
        sql = COMMENT.sub("", path.read_text(encoding="utf-8"))
        tables.update(name.lower() for name in CREATE_TABLE.findall(sql))
        for name, table, listing in CREATE_INDEX.findall(sql):
            indexes.append((name.lower(), table.lower(), _columns(listing)))
    return tables, indexes


def test_the_parser_sees_the_init_sql():
    # Without this, a pattern that stopped matching would pass the test below.
    tables, indexes = _init_sql()
    assert {"workflow_runs", "users", "episodic_distil_markers"} <= tables
    assert (
        "idx_workflow_runs_live",
        "workflow_runs",
        ("workflow_id", "started_at"),
    ) in indexes


def test_model_indexes_on_tables_the_init_sql_creates_come_from_the_sql():
    tables, indexes = _init_sql()
    names = {name for name, _, _ in indexes}
    shapes = {(table, columns) for _, table, columns in indexes}
    missing = [
        f"{table.name}.{index.name} ({', '.join(c.name for c in index.columns)})"
        for table in Base.metadata.sorted_tables
        if table.name in tables
        for index in sorted(table.indexes, key=lambda i: i.name)
        if index.name not in names
        and (table.name, tuple(c.name for c in index.columns)) not in shapes
    ]
    assert not missing, (
        "The init SQL creates these tables but not these model indexes, so "
        "compose and Helm installs never get them: create_all skips a table "
        "that exists. Add them in a new infra/database/init/ file.\n"
        + "\n".join(missing)
    )
