"""Unit tests for parse_connection_string / DatabaseConfig DSN resolution."""

import pytest

from database.connection import DatabaseConfig, parse_connection_string

pytestmark = [pytest.mark.unit, pytest.mark.database]


def test_parses_all_parts():
    p = parse_connection_string("postgresql://user:pw@db.example:6000/vigil")
    assert (p.host, p.port, p.database, p.user, p.password) == (
        "db.example",
        6000,
        "vigil",
        "user",
        "pw",
    )


def test_postgres_scheme_alias_and_default_port():
    p = parse_connection_string("postgres://u:p@h/db")
    assert p.port == 5432


def test_percent_encoded_credentials_are_decoded():
    """urlsplit does not decode; get_database_url re-quotes. Skip the unquote
    here and every password containing @ or / authenticates wrong."""
    p = parse_connection_string("postgresql://u%40corp:p%40ss%2Fword@h/db")
    assert p.user == "u@corp"
    assert p.password == "p@ss/word"


def test_encoded_password_round_trips_through_url():
    cfg = DatabaseConfig(connection_string="postgresql://me:p%40ss@h/db")
    assert cfg.password == "p@ss"
    assert "p%40ss" in cfg.get_database_url()


def test_allowed_query_params_are_kept():
    p = parse_connection_string(
        "postgresql://u:p@h/db?sslmode=require&connect_timeout=3"
    )
    assert p.query == {"sslmode": "require", "connect_timeout": "3"}


def test_sslmode_reaches_the_url_and_extras_are_serialized():
    cfg = DatabaseConfig(
        connection_string="postgresql://u:p@h/db?sslmode=require&application_name=vigil"
    )
    url = cfg.get_database_url()
    assert "sslmode=require" in url
    assert "application_name=vigil" in url


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "   ",
        "mysql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",  # no async engine exists in this repo
        "postgresql://u:p@h:notaport/db",
        "postgresql://u:p@h/",  # no database
        "postgresql://u:p@/db",  # no host
    ],
)
def test_rejects_malformed(dsn):
    with pytest.raises(ValueError):
        parse_connection_string(dsn)


@pytest.mark.parametrize(
    "param",
    [
        "sslcert=/etc/passwd",
        "sslkey=/k.pem",
        "sslrootcert=/ca.pem",
        "passfile=/etc/shadow",
        "service=prod",
        "sslpassword=hunter2",
    ],
)
def test_rejects_local_file_params(param):
    """These libpq params take local file paths: honouring one from a
    user-supplied DSN would be a file-read/probe primitive on the backend."""
    with pytest.raises(ValueError):
        parse_connection_string(f"postgresql://u:p@h/db?{param}")


def test_rejects_unknown_param_not_on_allowlist():
    with pytest.raises(ValueError):
        parse_connection_string("postgresql://u:p@h/db?some_new_thing=1")


def test_rejects_unix_socket_host():
    with pytest.raises(ValueError):
        parse_connection_string("postgresql://u:p@%2Fvar%2Frun/db")


def test_config_falls_back_to_env_on_bad_dsn(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "envhost")
    monkeypatch.setenv("POSTGRES_DB", "envdb")
    cfg = DatabaseConfig(connection_string="postgresql://u:p@h/db?sslcert=/etc/passwd")
    assert cfg.source == "env"
    assert cfg.host == "envhost"
    assert cfg.database == "envdb"


def test_config_prefers_dsn_over_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "envhost")
    cfg = DatabaseConfig(connection_string="postgresql://u:p@dsnhost/db")
    assert cfg.source == "connection_string"
    assert cfg.host == "dsnhost"


def test_config_uses_env_when_no_dsn(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "envhost")
    cfg = DatabaseConfig(connection_string="")
    assert cfg.source == "env"
    assert cfg.host == "envhost"


def test_env_connection_string_does_not_outrank_postgres_vars(monkeypatch):
    """backend/main.py exports a hardcoded default POSTGRESQL_CONNECTION_STRING
    for the MCP servers whenever the secret is unset. Resolving the DSN through
    the secrets manager's env fallback would let that default silently pin an
    operator's POSTGRES_* config to localhost."""
    monkeypatch.setenv(
        "POSTGRESQL_CONNECTION_STRING",
        "postgresql://deeptempo:pw@localhost:5432/deeptempo_soc",
    )
    monkeypatch.setenv("POSTGRES_HOST", "db.prod.internal")
    monkeypatch.setenv("POSTGRES_DB", "prod_db")
    cfg = DatabaseConfig()
    assert cfg.source == "env"
    assert cfg.host == "db.prod.internal"
    assert cfg.database == "prod_db"
