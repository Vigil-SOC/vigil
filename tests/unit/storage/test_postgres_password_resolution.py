"""A missing POSTGRES_PASSWORD fails closed outside DEV_MODE (#1566)."""

import logging
import os
from urllib.parse import unquote, urlsplit

import pytest

import core.storage.connection as connection
from core.backup.connection import backup_database_config
from core.config import get_settings
from core.storage.connection import (
    DatabaseConfig,
    MissingPostgresPasswordError,
    resolve_postgres_password,
)

SHIPPED = "deeptempo_secure_password_change_me"


@pytest.fixture(autouse=True)
def _env_only_secrets(monkeypatch):
    # Environment only: a developer's secrets.enc must not leak into the result.
    monkeypatch.setattr(connection, "get_secret", lambda key: os.environ.get(key))
    monkeypatch.setattr(connection, "_shipped_default_logged", False)
    monkeypatch.delenv("VIGIL_BACKUP_OWNER_CONNECTION", raising=False)


def _dev_mode(monkeypatch, on: bool):
    monkeypatch.setenv("DEV_MODE", "true" if on else "false")
    get_settings.cache_clear()


def test_missing_password_outside_dev_mode_raises(monkeypatch):
    _dev_mode(monkeypatch, False)
    monkeypatch.delenv("POSTGRES_PASSWORD")
    with pytest.raises(MissingPostgresPasswordError) as exc:
        DatabaseConfig()
    assert "POSTGRES_PASSWORD" in str(exc.value)
    assert "POSTGRESQL_CONNECTION_STRING" in str(exc.value)
    assert isinstance(exc.value, RuntimeError)


def test_missing_password_in_dev_mode_falls_back_with_warning(monkeypatch, caplog):
    _dev_mode(monkeypatch, True)
    monkeypatch.delenv("POSTGRES_PASSWORD")
    with caplog.at_level(logging.WARNING, logger=connection.logger.name):
        assert DatabaseConfig().password == SHIPPED
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_dsn_password_is_not_subject_to_the_missing_check(monkeypatch):
    _dev_mode(monkeypatch, False)
    monkeypatch.delenv("POSTGRES_PASSWORD")
    config = DatabaseConfig(connection_string="postgresql://u:s3cret@db.example/x")
    assert config.password == "s3cret"


@pytest.mark.parametrize("password", [SHIPPED, "change-me-before-production"])
def test_shipped_default_is_logged_once_and_not_refused(monkeypatch, caplog, password):
    _dev_mode(monkeypatch, False)
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    with caplog.at_level(logging.ERROR, logger=connection.logger.name):
        assert resolve_postgres_password() == password
        assert resolve_postgres_password() == password
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_shipped_default_inside_a_dsn_is_logged(monkeypatch, caplog):
    _dev_mode(monkeypatch, False)
    with caplog.at_level(logging.ERROR, logger=connection.logger.name):
        DatabaseConfig(connection_string=f"postgresql://u:{SHIPPED}@db.example/x")
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_unique_password_logs_nothing(monkeypatch, caplog):
    _dev_mode(monkeypatch, False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "unique-value")
    with caplog.at_level(logging.WARNING, logger=connection.logger.name):
        DatabaseConfig()
    assert not caplog.records


def test_backup_owner_connection_prefers_env_then_resolver(monkeypatch):
    monkeypatch.setenv("VIGIL_BACKUP_OWNER_CONNECTION", "1")
    _dev_mode(monkeypatch, False)

    monkeypatch.setenv("POSTGRES_PASSWORD", "owner-pw")
    assert backup_database_config().password == "owner-pw"

    monkeypatch.delenv("POSTGRES_PASSWORD")
    with pytest.raises(MissingPostgresPasswordError):
        backup_database_config()

    _dev_mode(monkeypatch, True)
    assert backup_database_config().password == SHIPPED


def test_backup_owner_connection_keeps_special_characters(monkeypatch):
    monkeypatch.setenv("VIGIL_BACKUP_OWNER_CONNECTION", "1")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd")
    config = backup_database_config()
    assert unquote(urlsplit(config.get_database_url()).password) == "p@ss:w/rd"
