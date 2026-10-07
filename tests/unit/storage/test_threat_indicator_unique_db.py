"""threat_indicators is unique per (source, type, value) however it was built.

``infra/database/init/14_threat_indicators.sql`` declares the constraint. The
ORM used to declare only indexes, so a database built by ``create_all`` -- the
one tests and some deployments use -- accepted duplicates the SQL-built one
rejects. The first test pins the model to the SQL without a server; the others
read the throwaway database ``create_all`` built.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect, text
from sqlalchemy.exc import IntegrityError

from core.storage.connection import get_db_manager
from core.storage.models import ThreatIndicator

ROOT = Path(__file__).resolve().parents[3]
INIT_SQL = ROOT / "infra" / "database" / "init" / "14_threat_indicators.sql"
NAME = "threat_indicators_unique"
COLUMNS = ["source", "indicator_type", "indicator_value"]

INSERT = text(
    "INSERT INTO threat_indicators (source, indicator_type, indicator_value) "
    "VALUES ('test-unique', 'ip', '198.51.100.7')"
)


def test_model_declares_the_init_sql_constraint():
    match = re.search(
        rf"CONSTRAINT\s+{NAME}\s+UNIQUE\s*\(([^)]*)\)", INIT_SQL.read_text()
    )
    assert match, f"{INIT_SQL.name} no longer declares {NAME}"
    sql_columns = [c.strip() for c in match.group(1).split(",")]

    declared = {
        c.name: [col.name for col in c.columns]
        for c in ThreatIndicator.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert declared.get(NAME) == sql_columns


@pytest.mark.external_service
@pytest.mark.database
def test_create_all_builds_the_unique_constraint():
    constraints = inspect(get_db_manager().engine).get_unique_constraints(
        "threat_indicators"
    )
    assert {"name": NAME, "column_names": COLUMNS} in [
        {"name": c["name"], "column_names": c["column_names"]} for c in constraints
    ]


@pytest.mark.external_service
@pytest.mark.database
def test_create_all_database_rejects_a_duplicate_indicator():
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(INSERT)
            with pytest.raises(IntegrityError, match=NAME):
                conn.execute(INSERT)
        finally:
            tx.rollback()
