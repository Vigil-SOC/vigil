"""Server-side session timeouts on the platform engine (#1443).

The platform engine used to bound only connection setup. A runaway query, or a
connection left idle inside an open transaction, held its locks and a pool slot
until the process exited. DB_STATEMENT_TIMEOUT_MS and
DB_IDLE_IN_TRANSACTION_TIMEOUT_MS are now sent as libpq ``options`` on every
connection the engine opens.
"""

import copy

import pytest
from sqlalchemy import text

from core.config import Settings
from core.storage.connection import (
    DatabaseConfig,
    DatabaseManager,
    _engine_connect_args,
)


def _build(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = DatabaseConfig(connection_string="postgresql://u:p@db.example.com/x")
    engine, proxy = DatabaseManager()._build(config, echo=False)
    assert proxy is None
    return config, engine


def _engine_kwargs(monkeypatch, **env):
    """Build through the real factory and capture what reaches create_engine."""
    import core.storage.connection as connection

    captured = {}
    real_create_engine = connection.create_engine

    def spy(url, **kwargs):
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(connection, "create_engine", spy)
    if "PGOPTIONS" not in env:
        monkeypatch.delenv("PGOPTIONS", raising=False)
    _, engine = _build(monkeypatch, **env)
    engine.dispose()
    return captured


def test_defaults():
    settings = Settings()
    assert settings.db_statement_timeout_ms == 0
    assert settings.db_idle_in_transaction_timeout_ms == 300000


def test_default_engine_bounds_idle_in_transaction_only(monkeypatch):
    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", raising=False)
    kwargs = _engine_kwargs(monkeypatch)
    assert kwargs["connect_args"] == {
        "connect_timeout": 5,
        "options": "-c idle_in_transaction_session_timeout=300000",
    }


def test_both_timeouts_reach_the_engine(monkeypatch):
    kwargs = _engine_kwargs(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="45000",
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="60000",
    )
    assert kwargs["connect_args"] == {
        "connect_timeout": 5,
        "options": (
            "-c statement_timeout=45000" " -c idle_in_transaction_session_timeout=60000"
        ),
    }


def test_zero_omits_the_option(monkeypatch):
    kwargs = _engine_kwargs(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="0",
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="0",
    )
    # connect_timeout survives; with nothing to set, options is not sent at all.
    assert kwargs["connect_args"] == {"connect_timeout": 5}


def test_negative_is_treated_as_disabled(monkeypatch):
    kwargs = _engine_kwargs(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="-1",
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="1000",
    )
    assert kwargs["connect_args"]["options"] == (
        "-c idle_in_transaction_session_timeout=1000"
    )


def test_pgoptions_is_carried_after_the_timeouts(monkeypatch):
    """An explicit ``options`` replaces libpq's PGOPTIONS fallback, which is how
    an eval selects a memory snapshot -- so the engine has to carry it through,
    last, so that an operator's own -c still wins."""
    kwargs = _engine_kwargs(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="0",
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="1000",
        PGOPTIONS="-c search_path=episodic_snap_x,public",
    )
    assert kwargs["connect_args"]["options"] == (
        "-c idle_in_transaction_session_timeout=1000"
        " -c search_path=episodic_snap_x,public"
    )


def test_pgoptions_alone_is_left_to_libpq(monkeypatch):
    kwargs = _engine_kwargs(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="0",
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="0",
        PGOPTIONS="-c search_path=episodic_snap_x,public",
    )
    assert "options" not in kwargs["connect_args"]


def test_non_postgres_url_gets_no_libpq_args(monkeypatch):
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "45000")
    config = DatabaseConfig(connection_string="postgresql://u:p@db.example.com/x")
    # connect_timeout and options are libpq parameters; sqlite3.connect() would
    # reject them.
    assert _engine_connect_args(config, "sqlite://") == {}


@pytest.mark.database
@pytest.mark.external_service
def test_server_reports_the_configured_timeouts(monkeypatch):
    """A connection from the real factory carries the timeouts server-side."""
    from core.storage.connection import get_db_manager

    config = copy.copy(get_db_manager().config)  # the throwaway test database
    config.statement_timeout_ms = 45000
    config.idle_in_transaction_timeout_ms = 60000
    engine, proxy = DatabaseManager()._build(config, echo=False)
    try:
        with engine.connect() as conn:
            stmt = conn.execute(text("SHOW statement_timeout")).scalar()
            idle = conn.execute(
                text("SHOW idle_in_transaction_session_timeout")
            ).scalar()
    finally:
        engine.dispose()
        if proxy is not None:
            proxy.close()
    assert stmt == "45s"
    assert idle == "1min"


@pytest.mark.database
@pytest.mark.external_service
def test_pgoptions_still_reaches_the_server_alongside_the_timeouts(monkeypatch):
    """PGOPTIONS keeps working, and wins a conflict with the configured value."""
    from core.storage.connection import get_db_manager

    monkeypatch.setenv(
        "PGOPTIONS", "-c search_path=pg_catalog,public -c statement_timeout=7000"
    )
    config = copy.copy(get_db_manager().config)
    config.statement_timeout_ms = 45000
    config.idle_in_transaction_timeout_ms = 60000
    engine, proxy = DatabaseManager()._build(config, echo=False)
    try:
        with engine.connect() as conn:
            path = conn.execute(text("SHOW search_path")).scalar()
            stmt = conn.execute(text("SHOW statement_timeout")).scalar()
            idle = conn.execute(
                text("SHOW idle_in_transaction_session_timeout")
            ).scalar()
    finally:
        engine.dispose()
        if proxy is not None:
            proxy.close()
    assert path.replace(" ", "") == "pg_catalog,public"
    assert stmt == "7s"
    assert idle == "1min"
