"""
Unit tests for DatabaseConfig.get_database_url().

Regression coverage for issue #306: passwords (and usernames) containing
URI-reserved characters must be percent-encoded so the resulting DSN parses
back to the original credentials instead of producing a malformed URI or
silent authentication failure.
"""

import pytest
from sqlalchemy.engine import make_url

from database.connection import DatabaseConfig


# Characters called out in issue #306 plus the other userinfo delimiters.
SPECIAL_PASSWORD = "p@ss/w0rd#?%&:=+ x"
SPECIAL_USER = "deep@tempo:user"


@pytest.fixture
def special_char_config(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", SPECIAL_USER)
    monkeypatch.setenv("POSTGRES_PASSWORD", SPECIAL_PASSWORD)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "deeptempo_soc")
    monkeypatch.setenv("POSTGRES_SSL_MODE", "prefer")
    return DatabaseConfig()


@pytest.mark.parametrize("async_driver", [False, True])
def test_special_chars_round_trip(special_char_config, async_driver):
    url = special_char_config.get_database_url(async_driver=async_driver)

    parsed = make_url(url)
    assert parsed.password == SPECIAL_PASSWORD
    assert parsed.username == SPECIAL_USER
    assert parsed.host == "db.example.com"
    assert parsed.port == 5432
    assert parsed.database == "deeptempo_soc"


def test_raw_special_chars_not_present_in_dsn(special_char_config):
    """The reserved chars must be escaped, not embedded literally."""
    url = special_char_config.get_database_url()

    userinfo = url.split("://", 1)[1].split("@", 1)[0]
    assert "@" not in userinfo  # the only literal @ is the userinfo delimiter
    assert "%40" in userinfo  # @ -> %40
    assert "%23" in userinfo  # # -> %23


def test_host_override_round_trips_credentials(special_char_config):
    """Proxy host/port overrides must not disturb credential encoding."""
    url = special_char_config.get_database_url(host="127.0.0.1", port=6543)

    parsed = make_url(url)
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 6543
    assert parsed.password == SPECIAL_PASSWORD
