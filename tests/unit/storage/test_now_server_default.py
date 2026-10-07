"""A server default that calls a function must reach Postgres as a call.

SQLAlchemy renders a plain-string ``server_default`` as a quoted literal, so
``server_default="now()"`` made ``create_all`` emit ``DEFAULT 'now()'``. Postgres
reads that as timestamp input and folds it once, at CREATE TABLE: the default
became the table's creation time, and every INSERT that omitted the column was
stamped with it. The ORM never showed it, because the models also set a
Python-side ``default=utcnow``. Raw SQL did: the ``05_case_management_extended.sql``
seed that compose re-applies after create_all. ``text("now()")`` renders
``DEFAULT now()``, which Postgres evaluates per row.

The DDL is compiled rather than the column attributes read, because the compiled
DDL is what ``create_all`` sends.
"""

import re

import pytest
from sqlalchemy import Column, DateTime, MetaData, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from core.storage.models import Base

pytestmark = pytest.mark.unit

# A function call inside quotes is a string literal to Postgres.
QUOTED_CALL = re.compile(r"DEFAULT '[A-Za-z_][A-Za-z0-9_.]*\(.*?\)'")


def _ddl(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_the_check_catches_the_string_form():
    # Without this, a SQLAlchemy that rendered the string some other way would
    # leave the test below unable to fail.
    table = Table(
        "t", MetaData(), Column("created_at", DateTime, server_default="now()")
    )
    assert QUOTED_CALL.search(_ddl(table))


def test_no_model_renders_a_function_call_as_a_string_default():
    offenders = [
        f"{table.name}: {line.strip().rstrip(',')}"
        for table in Base.metadata.sorted_tables
        for line in _ddl(table).splitlines()
        if QUOTED_CALL.search(line)
    ]
    assert not offenders, (
        "A plain-string server_default is a quoted literal, and Postgres folds "
        "DEFAULT 'now()' to the CREATE TABLE time. Write text(\"now()\").\n"
        + "\n".join(offenders)
    )
