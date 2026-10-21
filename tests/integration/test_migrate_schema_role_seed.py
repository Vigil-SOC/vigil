"""``seed_default_roles`` seeds ``roles`` from ``06_auth_tables.sql`` — #2587.

An older version of the migration seeded its own ``admin``/``analyst``/``viewer``
rows. Nothing reads their permission keys, and the legacy ``Viewer`` name
collides with the real seed on ``roles.name``, so ``role-admin`` never landed
and bootstrap could not create the first account. The migration now seeds the
five roles from the SQL file and removes the legacy rows no user holds.

These tests use a scratch database holding only ``roles`` and a minimal
``users``. ``create_all`` is deliberately not used: it needs ``pg_trgm`` for
the findings GIN index, and none of that is relevant here.
``test_migrate_schema_ownership.py`` covers the runner.
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCRATCH_DB = "vigil_test_role_seed"

# Taken verbatim from database/init/06_auth_tables.sql so the test exercises the
# real constraints — in particular the UNIQUE name the legacy rows collide on.
ROLES_DDL = """
CREATE TABLE IF NOT EXISTS roles (
    role_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    permissions JSONB NOT NULL DEFAULT '{}',
    is_system_role BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

# Just enough of users for its role_id FK.
USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(50) PRIMARY KEY,
    role_id VARCHAR(50) NOT NULL REFERENCES roles(role_id)
);
"""

AUTH_SQL = REPO_ROOT / "infra" / "database" / "init" / "06_auth_tables.sql"
EXPECTED_ROLE_IDS = set(
    re.findall(r"^\('(role-[\w-]+)'", AUTH_SQL.read_text(), re.MULTILINE)
)

# What the old migration seeded, names included: 'Viewer' is the collision.
LEGACY_ROWS = """
INSERT INTO roles (role_id, name, description, is_system_role) VALUES
('admin', 'Administrator', 'Full system access', true),
('analyst', 'Security Analyst', 'Can manage cases and findings', true),
('viewer', 'Viewer', 'Read-only access', true)
"""


def _url(database: str) -> str:
    """Build a DSN from the POSTGRES_* variables CI sets, else dev defaults.

    CI's integration job uses test/test/deeptempo_test; a developer's compose
    stack uses deeptempo/deeptempo_secure_password_change_me. Hardcoding either
    makes the test silently skip in the other environment.
    """
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


# 'postgres' always exists and is never the target, so it is safe to CREATE and
# DROP the scratch database from.
ADMIN_URL = _url("postgres")
SCRATCH_URL = _url(SCRATCH_DB)


def _postgres_available() -> bool:
    try:
        eng = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        with eng.connect():
            return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _postgres_available(),
        reason="requires a local PostgreSQL (docker compose up -d postgres)",
    )
)


def _load_migrate_schema():
    """Import scripts/migrate_schema.py by path.

    ``scripts/`` is not a package, and the file is written as a CLI entry point,
    so there is no importable module name for it. Loading by location is the
    only way to exercise a single migration in isolation.
    """
    path = REPO_ROOT / "scripts" / "migrate_schema.py"
    spec = importlib.util.spec_from_file_location("vigil_migrate_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def empty_roles_db():
    """A scratch database with empty ``roles`` and ``users`` tables."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(SCRATCH_URL)
    with scratch.connect() as c:
        c.execute(text(ROLES_DDL))
        c.execute(text(USERS_DDL))
        c.commit()

    yield scratch

    scratch.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _migrate(engine, setup=()):
    migrate_schema = _load_migrate_schema()
    with engine.connect() as conn:
        for statement in setup:
            conn.execute(text(statement))
        conn.commit()
        migrate_schema.seed_default_roles(conn)
        conn.commit()
        return set(conn.execute(text("SELECT role_id FROM roles")).scalars())


def test_expected_ids_read_from_the_sql_file():
    assert len(EXPECTED_ROLE_IDS) == 5 and "role-admin" in EXPECTED_ROLE_IDS


def test_empty_table_gets_the_five_roles(empty_roles_db):
    assert _migrate(empty_roles_db) == EXPECTED_ROLE_IDS


def test_unheld_legacy_rows_are_replaced_by_the_five(empty_roles_db):
    assert _migrate(empty_roles_db, [LEGACY_ROWS]) == EXPECTED_ROLE_IDS


def test_held_legacy_row_stays_and_nothing_is_seeded(empty_roles_db):
    rows = _migrate(
        empty_roles_db,
        [LEGACY_ROWS, "INSERT INTO users VALUES ('u1', 'viewer')"],
    )
    assert rows == {"viewer"}


def test_second_run_changes_nothing(empty_roles_db):
    _migrate(empty_roles_db)
    with empty_roles_db.connect() as conn:
        conn.execute(text("DELETE FROM roles WHERE role_id = 'role-viewer'"))
        conn.commit()
    # A populated table is left alone, so a role an operator deleted stays gone.
    assert _migrate(empty_roles_db) == EXPECTED_ROLE_IDS - {"role-viewer"}
