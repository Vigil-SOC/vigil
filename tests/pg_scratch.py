"""A schema of its own on the database ``VIGIL_TEST_PG_DSN`` names, for a test
that builds its own tables. It never drops or empties the database's tables,
which other Postgres tests share, and it is dropped after the test (#4095).

The DSN takes the form the other Postgres tests take (``postgresql://...``);
it is read through psycopg2, the driver Vigil ships. Make the tables with
``create_tables``, so they land in the scratch schema."""

import os
import uuid
from contextlib import contextmanager

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@contextmanager
def scratch_engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(DSN).set(drivername="postgresql+psycopg2")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def create_tables(engine, tables=None):
    """Create Vigil's tables (or these ``tables``) in the scratch schema. Not
    checkfirst: ``public`` is on the search path (for extensions such as
    pg_trgm), so the check would find the database's own tables and skip them,
    and the test would then write into those."""
    from core.storage.models import Base

    Base.metadata.create_all(engine, tables=tables, checkfirst=False)
