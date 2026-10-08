"""DB-backed unit tests must not write to the database a human is using.

`TestApprovalQueue` used to leave three pending containment proposals in
whatever `DATABASE_URL` pointed at, once per run: 93 rows had accumulated in a
development database by the time anyone noticed, indistinguishable in the
operator's approval queue from a real proposal (#747).

The guarantee these tests assert is structural rather than per-test hygiene: a
unit test that touches the database is handed a throwaway one, so a test that
forgets to clean up cannot reach the developer's data.
"""

import os
import subprocess
import sys
from pathlib import Path

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


# Regression for #1849. The leak needs an unmarked test to run first in the same
# process, so the pair below runs in a child pytest, in that order, and the
# parent asserts the child passes. Inert unless the parent sets the flag.
_INNER = "VIGIL_DB_ISOLATION_INNER"
_inner_only = pytest.mark.skipif(not os.environ.get(_INNER), reason="child run only")


@_inner_only
def test_inner_unmarked_test_leaves_engine_on_blocked_host():
    from core.storage.connection import get_db_manager

    # What any unmarked test calling initialize() leaves behind.
    get_db_manager().initialize()
    assert "postgres-blocked-in-unit-tests" in str(get_db_manager().engine.url)


@_inner_only
@pytest.mark.database
@pytest.mark.external_service
def test_inner_throwaway_database_is_reachable():
    from sqlalchemy import text

    from core.storage.connection import get_db_manager

    with get_db_manager().engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


@pytest.mark.database
@pytest.mark.external_service
def test_throwaway_database_survives_an_engine_left_on_a_blocked_host():
    this = Path(__file__).as_posix()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{this}::test_inner_unmarked_test_leaves_engine_on_blocked_host",
            f"{this}::test_inner_throwaway_database_is_reachable",
        ],
        env={**os.environ, _INNER: "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
