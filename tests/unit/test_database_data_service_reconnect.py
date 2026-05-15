"""Tests for DatabaseDataService recovery from transient Postgres outages.

Before the fix, a Postgres outage at backend startup would set
`_use_json_fallback = True` permanently, trapping the singleton in
JSON-file-fallback for the rest of the process lifetime even after
Postgres came back. These tests verify the rate-limited auto-reconnect.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from services.database_data_service import DatabaseDataService


def _make_service_in_fallback() -> DatabaseDataService:
    """Construct a service that failed its initial DB connection."""
    with patch(
        "services.database_data_service.init_database",
        side_effect=RuntimeError("postgres down"),
    ):
        svc = DatabaseDataService()
    assert svc._db_connected is False
    assert svc._use_json_fallback is True
    return svc


def test_db_available_retries_when_disconnected_after_interval():
    svc = _make_service_in_fallback()
    # Pretend the cooldown has elapsed so the next read triggers a retry.
    svc._last_reconnect_attempt = 0.0

    fake_manager = MagicMock()
    fake_manager.health_check.return_value = True

    with patch("services.database_data_service.init_database") as fake_init, patch(
        "services.database_data_service.get_db_manager", return_value=fake_manager
    ), patch("services.database_data_service.DatabaseService"):
        assert svc._db_available is True
        fake_init.assert_called_once()

    assert svc._db_connected is True
    assert svc._use_json_fallback is False


def test_db_available_rate_limits_reconnect_attempts():
    svc = _make_service_in_fallback()
    # Force a recent attempt so the cooldown should suppress the next try.
    svc._last_reconnect_attempt = float("inf")

    with patch("services.database_data_service.init_database") as fake_init:
        assert svc._db_available is False
        fake_init.assert_not_called()


def test_db_available_short_circuits_when_already_connected():
    """When already connected, reading the property must NOT touch init_database."""
    fake_manager = MagicMock()
    fake_manager.health_check.return_value = True
    with patch("services.database_data_service.init_database"), patch(
        "services.database_data_service.get_db_manager", return_value=fake_manager
    ), patch("services.database_data_service.DatabaseService"):
        svc = DatabaseDataService()

    assert svc._db_connected is True

    with patch("services.database_data_service.init_database") as fake_init:
        assert svc._db_available is True
        fake_init.assert_not_called()


def test_demo_mode_never_attempts_reconnect():
    with patch("services.database_data_service.is_demo_mode", return_value=True), patch(
        "services.database_data_service.get_demo_service", create=True
    ):
        # Stub out the demo service factory used inside the constructor.
        import services.database_data_service as mod

        mod.get_demo_service = lambda: MagicMock()  # type: ignore[attr-defined]
        svc = DatabaseDataService()

    svc._last_reconnect_attempt = 0.0
    with patch("services.database_data_service.init_database") as fake_init:
        assert svc._db_available is False
        fake_init.assert_not_called()
