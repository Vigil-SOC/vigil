"""DB-backed unit tests must not write to the database a human is using.

`TestApprovalQueue` used to leave three pending containment proposals in
whatever `DATABASE_URL` pointed at, once per run: 93 rows had accumulated in a
development database by the time anyone noticed, indistinguishable in the
operator's approval queue from a real proposal (#747).

The guarantee these tests assert is structural rather than per-test hygiene: a
unit test that touches the database is handed a throwaway one, so a test that
forgets to clean up cannot reach the developer's data.
"""

import pytest


@pytest.mark.database
@pytest.mark.external_service
def test_db_marked_tests_run_against_a_throwaway_database():
    from core.storage.connection import DatabaseConfig, get_db_manager

    configured = DatabaseConfig().database  # what the environment points at
    active = get_db_manager().config.database

    assert active != configured, (
        f"DB-backed unit tests are writing to {active!r}, the database the "
        "environment points at"
    )
    assert active.startswith("vigil_test_")


@pytest.mark.database
@pytest.mark.external_service
def test_the_throwaway_database_carries_the_orm_schema():
    """create_all provisions it from the models, so writes have somewhere to go."""
    from sqlalchemy import inspect

    from core.storage.connection import get_db_manager

    tables = set(inspect(get_db_manager().engine).get_table_names())
    assert {
        "approval_actions",
        "system_config",
        "findings",
        "intake_triggers",
    } <= tables
