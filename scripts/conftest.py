"""
Pytest configuration for scripts directory.

conftest.py for scripts/.
Provides the 'config' pytest fixture used by test_splunk_claude_integration.py,
and a database initialization fixture for SLA assignment tests.
"""

import os
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def initialize_db():
    """Initialize the database manager before each test that uses get_db_session().

    In a test environment without a live PostgreSQL instance the real
    DatabaseManager.initialize() cannot connect.  This fixture directly
    sets ``_session_factory`` on the singleton DatabaseManager so that
    get_db_session() succeeds and returns a MagicMock session, satisfying
    the "Database not initialized" check without making any network calls.
    """
    from database.connection import get_db_manager

    mock_session = MagicMock()
    # Make query(...).filter(...).all() return an empty list by default
    mock_session.query.return_value.filter.return_value.all.return_value = []

    db_manager = get_db_manager()
    original_engine = db_manager._engine
    original_factory = db_manager._session_factory

    # Provide a minimal engine stub and a session factory that returns the mock
    db_manager._engine = MagicMock()
    db_manager._session_factory = MagicMock(return_value=mock_session)

    yield

    # Restore original state to avoid polluting other tests
    db_manager._engine = original_engine
    db_manager._session_factory = original_factory



@pytest.fixture
def config():
    """Provide Splunk connection configuration from environment variables.

    Reads SPLUNK_URL, SPLUNK_USERNAME, SPLUNK_PASSWORD, and optionally
    SPLUNK_VERIFY_SSL from the environment (or a loaded .env file).
    Skips the test if the required variables are not set.
    """
    url = os.environ.get("SPLUNK_URL")
    username = os.environ.get("SPLUNK_USERNAME")
    password = os.environ.get("SPLUNK_PASSWORD")

    if not url or not username or not password:
        pytest.skip(
            "Splunk credentials not configured. "
            "Set SPLUNK_URL, SPLUNK_USERNAME, and SPLUNK_PASSWORD in the "
            "environment or in a .env file to run this test."
        )

    verify_ssl_raw = os.environ.get("SPLUNK_VERIFY_SSL", "false")
    verify_ssl = verify_ssl_raw.lower() in ("1", "true", "yes")

    return {
        "url": url,
        "username": username,
        "password": password,
        "verify_ssl": verify_ssl,
    }
