"""Which database create and restore connect as.

``DatabaseConfig()`` reads ``POSTGRESQL_CONNECTION_STRING`` from secrets.enc
before ``POSTGRES_*``. Under Compose that DSN is ``vigil_app``, which cannot
``CREATE DATABASE``. Set ``VIGIL_BACKUP_OWNER_CONNECTION=1`` to use the
cluster owner from ``POSTGRES_*`` instead. Unset, the call stays
``DatabaseConfig()`` with no argument.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from core.config import get_settings
from core.storage.connection import DatabaseConfig, resolve_postgres_password

OWNER_CONNECTION_ENV = "VIGIL_BACKUP_OWNER_CONNECTION"
_ON = frozenset({"1", "true", "yes", "on"})


def backup_database_config() -> DatabaseConfig:
    raw = os.environ.get(OWNER_CONNECTION_ENV, "").strip().lower()  # noqa: ENV001
    if raw not in _ON:
        return DatabaseConfig()
    settings = get_settings()
    user = quote(settings.postgres_user, safe="")
    password = quote(_owner_password(), safe="")
    host = quote(settings.postgres_host, safe="")
    database = quote(settings.postgres_db, safe="")
    sslmode = quote(settings.postgres_ssl_mode, safe="")
    dsn = (
        f"postgresql://{user}:{password}@{host}:{settings.postgres_port}"
        f"/{database}?sslmode={sslmode}"
    )
    return DatabaseConfig(connection_string=dsn)


def _owner_password() -> str:
    # Compose puts the cluster password in the environment. A value in
    # secrets.enc is the app role's store and must not outrank that.
    env = os.environ.get("POSTGRES_PASSWORD")  # noqa: ENV001
    if env:
        return env
    return resolve_postgres_password()
