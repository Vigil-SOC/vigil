"""Tests for DatabaseDataService recovery from transient Postgres outages.

These tests verify the rate-limited auto-reconnect when the initial
connection fails.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.storage.database_data_service import DatabaseDataService


def _make_disconnected_service() -> DatabaseDataService:
    """Construct a service whose first connection attempt (on first use) failed."""
    svc = DatabaseDataService()
    with patch(
        "core.storage.database_data_service.init_database",
        side_effect=RuntimeError("postgres down"),
    ):
        assert svc._db_available is False
    assert svc._db_connected is False
    return svc


def test_construction_opens_no_connection():
    """Importing a module with a module-level service must not touch Postgres (#1456)."""
    with patch("core.storage.database_data_service.init_database") as fake_init:
        svc = DatabaseDataService()
    fake_init.assert_not_called()
    assert svc._db_connected is False


def test_first_access_connects_even_on_a_freshly_booted_host():
    """The cooldown must not suppress the first attempt when monotonic() is small."""
    fake_manager = MagicMock()
    fake_manager.health_check.return_value = True
    svc = DatabaseDataService()
    with patch("core.storage.database_data_service.time.monotonic", return_value=1.0):
        with patch("core.storage.database_data_service.init_database") as fake_init:
            with patch(
                "core.storage.database_data_service.get_db_manager",
                return_value=fake_manager,
            ), patch("core.storage.database_data_service.DatabaseService"):
                assert svc._db_available is True
    fake_init.assert_called_once()


def test_db_available_retries_when_disconnected_after_interval():
    svc = _make_disconnected_service()
    # Pretend the cooldown has elapsed so the next read triggers a retry.
    svc._last_reconnect_attempt = 0.0

    fake_manager = MagicMock()
    fake_manager.health_check.return_value = True

    with patch("core.storage.database_data_service.init_database") as fake_init, patch(
        "core.storage.database_data_service.get_db_manager", return_value=fake_manager
    ), patch("core.storage.database_data_service.DatabaseService"):
        assert svc._db_available is True
        fake_init.assert_called_once()

    assert svc._db_connected is True


def test_db_available_rate_limits_reconnect_attempts():
    svc = _make_disconnected_service()
    # Force a recent attempt so the cooldown should suppress the next try.
    svc._last_reconnect_attempt = float("inf")

    with patch("core.storage.database_data_service.init_database") as fake_init:
        assert svc._db_available is False
        fake_init.assert_not_called()


def test_db_available_short_circuits_when_already_connected():
    """When already connected, reading the property must NOT touch init_database."""
    fake_manager = MagicMock()
    fake_manager.health_check.return_value = True
    svc = DatabaseDataService()
    with patch("core.storage.database_data_service.init_database"), patch(
        "core.storage.database_data_service.get_db_manager", return_value=fake_manager
    ), patch("core.storage.database_data_service.DatabaseService"):
        assert svc._db_available is True

    assert svc._db_connected is True

    with patch("core.storage.database_data_service.init_database") as fake_init:
        assert svc._db_available is True
        fake_init.assert_not_called()


def test_demo_mode_never_attempts_reconnect():
    with patch("core.storage.database_data_service.is_demo_mode", return_value=True):
        # The demo service is injected, so the constructor builds no real one.
        svc = DatabaseDataService(demo_data=MagicMock())

    svc._last_reconnect_attempt = 0.0
    with patch("core.storage.database_data_service.init_database") as fake_init:
        assert svc._db_available is False
        fake_init.assert_not_called()
