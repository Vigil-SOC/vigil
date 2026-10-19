"""create_tables() must bring up a bare database, including pg_trgm (#2428).

Provisions an empty scratch database WITHOUT pre-creating any extension; the
findings GIN index (gin_trgm_ops) fails unless create_tables() enables pg_trgm.
Reuses the loopback-only guards of the schema-drift tests since it DROPs a DB.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.storage.connection import DatabaseManager
from tests.integration.test_schema_drift_upgrade import (
    _CONNECT_ARGS,
    _admin_engine,
    _url,
    postgres_available,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.integration, pytest.mark.database]

SCRATCH_DB = "vigil_test_create_tables_ext"


def test_create_tables_enables_pg_trgm_on_bare_database(postgres_available):  # noqa: F811
    admin = _admin_engine()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    scratch = create_engine(_url(SCRATCH_DB), connect_args=_CONNECT_ARGS)
    try:
        has_ext = text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        with scratch.connect() as c:
            assert c.execute(has_ext).first() is None

        with pytest.MonkeyPatch.context() as mp:
            dm = DatabaseManager()
            mp.setattr(dm, "_engine", scratch)
            mp.setattr(dm, "_session_factory", sessionmaker(bind=scratch))
            dm.create_tables()
            with scratch.connect() as c:
                assert c.execute(has_ext).first() is not None
            dm.create_tables()  # idempotent
    finally:
        scratch.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()
