#!/usr/bin/env python3
"""Write the committed column listing for a freshly provisioned database.

A patch release may not change tables or columns: backup restore accepts any
backup from the same major.minor, and re-provisioning afterwards recreates
indexes. The listing is taken from a scratch database built the way Compose
does — tolerant apply of ``infra/database/init/*.sql`` (``00_apply.sh``),
``init_database()``, then the SQL again — so SQL-only tables and the
nullability fixes that run after ``create_all`` are in it. Indexes,
constraints and comments are not.

``tests/integration/test_schema_snapshot.py`` rebuilds the listing and fails
if it drifts. To change the schema on purpose, run this and commit the diff:

    python scripts/generate_schema_snapshot.py
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "core" / "storage" / "schema.snapshot.json"
INIT_DIR = ROOT / "infra" / "database" / "init"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.storage.connection import (  # noqa: E402
    get_db_manager,
    init_database,
    reset_schema_drift_check,
)
from scripts.generate_api_v1_contract import serialize  # noqa: E402

# CREATE/DROP is only safe against a loopback server. Same set as
# tests/integration/test_schema_drift_upgrade.py.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"}
_CONNECT_ARGS = {"connect_timeout": 5}


class ScratchDatabaseUnavailable(RuntimeError):
    """No local Postgres to provision. Tests skip; the script exits non-zero."""


def _host() -> str:
    return os.getenv("POSTGRES_HOST", "localhost")


def _params() -> tuple[str, str, str, str]:
    """User, password, host, port from POSTGRES_*, the knobs DatabaseManager reads.

    DATABASE_URL is not one of them. CI's integration job uses test/test; a
    compose stack uses the deeptempo defaults.
    """
    return (
        os.getenv("POSTGRES_USER", "deeptempo"),
        os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"),
        _host(),
        os.getenv("POSTGRES_PORT", "5432"),
    )


def _url(database: str) -> URL:
    user, password, host, port = _params()
    return URL.create(
        "postgresql+psycopg2",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=database,
    )


def _admin_engine():
    return create_engine(
        _url("postgres"), isolation_level="AUTOCOMMIT", connect_args=_CONNECT_ARGS
    )


def postgres_skip_reason() -> str | None:
    """Why a scratch database must not be created here, or None when it may."""
    if _host() not in _LOCAL_HOSTS:
        return (
            f"refusing to CREATE/DROP a database on non-local POSTGRES_HOST {_host()!r}"
        )
    engine = _admin_engine()
    try:
        with engine.connect():
            return None
    except Exception as e:  # noqa: BLE001
        return f"requires a local PostgreSQL: {e}"
    finally:
        engine.dispose()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _apply_init_sql(database: str) -> None:
    """Tolerant apply, matching infra/docker/initdb/00_apply.sh.

    ON_ERROR_STOP=0 and a per-file ``|| true``: files that target tables
    ``create_all`` has not built yet fail on the first pass and succeed on
    the second. psql is what the compose hook runs; a statement splitter
    would not reproduce its transaction-abort behaviour.
    """
    user, password, host, port = _params()
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": host,
            "PGPORT": port,
            "PGUSER": user,
            "PGPASSWORD": password,
            "PGDATABASE": database,
        }
    )
    for path in sorted(INIT_DIR.glob("*.sql")):
        try:
            completed = subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=0", "-q", "-f", str(path)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise ScratchDatabaseUnavailable(
                f"psql is required to apply init SQL: {e}"
            ) from e
        # ON_ERROR_STOP=0 still exits 0 when a statement fails. Non-zero means
        # psql never ran the file (auth, no server), so the listing would be a
        # lie about a database the SQL did not touch.
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"psql failed on {path.name}: {detail}")


def column_listing(connection) -> dict:
    """Every public base table's columns: data type and nullability.

    ``format_type`` keeps length and array element type, which
    ``information_schema.columns.data_type`` drops. Indexes are rows in
    ``pg_class`` with a different relkind, so an index-only change is absent
    here. No constraints, no comments.
    """
    rows = connection.execute(text("""
            SELECT c.relname, a.attname,
                   format_type(a.atttypid, a.atttypmod),
                   NOT a.attnotnull
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND NOT c.relispartition
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY c.relname, a.attname
            """))
    listing: dict = {}
    for table, column, data_type, nullable in rows:
        listing.setdefault(table, {})[column] = {
            "data_type": data_type,
            "nullable": bool(nullable),
        }
    return listing


def _init_on(engine) -> None:
    """Point the process singleton at ``engine`` for ``init_database()`` only.

    DatabaseManager reads POSTGRES_* through its config and ignores
    DATABASE_URL, and ``initialize()`` keeps the engine it already has. The
    singleton is process-wide, so it has to be pointed back before the
    scratch database is dropped.
    """
    manager = get_db_manager()
    saved_engine = manager._engine
    saved_factory = manager._session_factory
    # A cached verdict belongs to whatever database was inspected last.
    reset_schema_drift_check()
    manager._engine = engine
    manager._session_factory = sessionmaker(bind=engine)
    try:
        init_database()
    finally:
        replaced = manager._engine
        manager._engine = saved_engine
        manager._session_factory = saved_factory
        reset_schema_drift_check()
        if (
            replaced is not None
            and replaced is not engine
            and replaced is not saved_engine
        ):
            replaced.dispose()


def build_listing() -> dict:
    """Provision a scratch database in compose order and list its columns."""
    reason = postgres_skip_reason()
    if reason:
        raise ScratchDatabaseUnavailable(reason)

    name = f"vigil_schema_snap_{os.getpid()}_{secrets.token_hex(3)}"
    ident = _quote_ident(name)
    admin = _admin_engine()
    engine = None
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {ident} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {ident}"))
        engine = create_engine(_url(name), connect_args=_CONNECT_ARGS)
        _apply_init_sql(name)
        _init_on(engine)
        _apply_init_sql(name)
        with engine.connect() as conn:
            return column_listing(conn)
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {ident} WITH (FORCE)"))
        admin.dispose()


def main() -> int:
    try:
        listing = build_listing()
    except ScratchDatabaseUnavailable as e:
        print(e, file=sys.stderr)
        return 1
    SNAPSHOT.write_text(serialize(listing), encoding="utf-8")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
