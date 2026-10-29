"""init_database() refuses a database whose tables carry no alembic revision."""

import pytest
from sqlalchemy import text

from core.storage.connection import (
    SchemaRevisionError,
    get_db_manager,
    init_database,
    stamp_schema_head,
)

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def test_init_database_refuses_tables_without_a_revision():
    engine = get_db_manager().engine
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    try:
        with pytest.raises(SchemaRevisionError, match="no alembic_version"):
            init_database()
    finally:
        stamp_schema_head(engine)
