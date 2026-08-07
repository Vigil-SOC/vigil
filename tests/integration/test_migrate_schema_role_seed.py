"""``seed_default_roles`` against an empty ``roles`` table — issue #567.

``scripts/migrate_schema.py`` binds the permissions payload as ``:perms::jsonb``.
SQLAlchemy's ``text()`` parser does not recognise a bind parameter immediately
followed by the ``::`` cast operator, so it converts the *other* parameters to
psycopg2's ``%(name)s`` style, leaves ``:perms::jsonb`` in the statement
verbatim, and omits ``perms`` from the bound parameters entirely. PostgreSQL
then receives a literal ``:`` and raises a syntax error.

The migration only runs its ``INSERT`` when ``roles`` is empty, which is why no
compose- or Helm-provisioned database hits it: ``database/init/06_auth_tables.sql``
seeds that table. Databases built by ``create_all`` alone — the storage_status
"create schema" endpoint, and dev/test databases — do hit it.

These tests use a scratch database containing *only* the ``roles`` table, taken
verbatim from ``database/init/06_auth_tables.sql``. ``create_all`` is
deliberately not used: it needs ``pg_trgm``/``vector`` for the findings GIN
index, and none of that is relevant to this statement.

The runner's shared-transaction defect (``scripts/migrate_schema.py:231``, one
``engine.begin()`` around all 13 migrations, so a single SQL failure discards the
whole run while still reporting migrations as applied) is out of scope here and
belongs with the migration-invocation work in #562. These tests call the
migration function directly, which is also why the exception surfaces at all —
``run_migrations`` catches per migration.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCRATCH_DB = "vigil_test_role_seed"

# Taken verbatim from database/init/06_auth_tables.sql so the test exercises the
# real column types — in particular `permissions JSONB`, which is what the cast
# in the migration exists for.
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

EXPECTED_ROLE_IDS = {"admin", "analyst", "viewer"}


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
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


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
    """A scratch database with an empty ``roles`` table and nothing else.

    Empty is the whole point: ``seed_default_roles`` returns early when the
    table has rows, so a seeded database never reaches the broken statement.
    """
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(SCRATCH_URL)
    with scratch.connect() as c:
        c.execute(text(ROLES_DDL))
        c.commit()

    yield scratch

    scratch.dispose()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def test_seed_default_roles_inserts_the_three_default_roles(empty_roles_db):
    """The migration runs to completion and the rows land.

    Before the fix this fails at the first INSERT with
    'syntax error at or near ":"' — the payload is never bound.
    """
    migrate_schema = _load_migrate_schema()

    with empty_roles_db.connect() as conn:
        migrate_schema.seed_default_roles(conn)
        conn.commit()

        rows = conn.execute(text("SELECT role_id FROM roles")).scalars().all()

    assert set(rows) == EXPECTED_ROLE_IDS


def test_seed_default_roles_stores_permissions_as_jsonb(empty_roles_db):
    """The permissions payload arrives as queryable JSONB, not a string.

    Asserting the row count alone would not catch a cast that binds but stores
    the wrong type, so this reads a key back out through the JSONB operator.
    """
    migrate_schema = _load_migrate_schema()

    with empty_roles_db.connect() as conn:
        migrate_schema.seed_default_roles(conn)
        conn.commit()

        admin_perms = conn.execute(
            text("SELECT permissions FROM roles WHERE role_id = 'admin'")
        ).scalar()

        # Through the JSONB operator, so this fails if the column holds a
        # JSON-encoded string rather than a JSONB object.
        via_operator = conn.execute(
            text(
                "SELECT permissions ->> 'manage_users' FROM roles WHERE role_id = 'admin'"
            )
        ).scalar()

    assert isinstance(admin_perms, dict)
    assert admin_perms["admin"] is True
    assert via_operator == "true"


def test_seed_default_roles_marks_defaults_as_system_roles(empty_roles_db):
    """`is_system_role` is bound too, and must not fall through to the default.

    The column defaults to FALSE, so an unbound parameter here would be
    invisible in a row-count assertion.
    """
    migrate_schema = _load_migrate_schema()

    with empty_roles_db.connect() as conn:
        migrate_schema.seed_default_roles(conn)
        conn.commit()

        flags = conn.execute(text("SELECT is_system_role FROM roles")).scalars().all()

    assert flags == [True, True, True]


def test_seed_default_roles_skips_a_populated_table(empty_roles_db):
    """Characterisation: a seeded table is left alone.

    This is why compose- and Helm-provisioned databases never hit the bug, and
    it must keep holding after the fix.
    """
    migrate_schema = _load_migrate_schema()

    with empty_roles_db.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO roles (role_id, name, description) "
                "VALUES ('role-admin', 'Administrator', 'pre-existing')"
            )
        )
        conn.commit()

        migrate_schema.seed_default_roles(conn)
        conn.commit()

        rows = conn.execute(text("SELECT role_id FROM roles")).scalars().all()

    assert rows == ["role-admin"]


def test_unfixed_paramstyle_would_leave_the_parameter_unbound(empty_roles_db):
    """Pin the mechanism, so a regression is diagnosable rather than just red.

    Re-creates the original statement inline. It must fail *and* the failure
    must be the unbound-parameter one, not some unrelated SQL error — the
    message names the stray ``:`` and psycopg2 reports only the parameters it
    did bind.
    """
    original = text("""
        INSERT INTO roles (role_id, name, description, permissions,
                           is_system_role, created_at, updated_at)
        VALUES (:role_id, :name, :desc, :perms::jsonb, :is_sys, now(), now())
        ON CONFLICT (role_id) DO NOTHING
        """)

    # perms is absent on purpose: text() never recognised it as a parameter, so
    # passing it raises a different error and hides the point.
    assert "perms" not in original._bindparams

    with empty_roles_db.connect() as conn:
        with pytest.raises(DBAPIError) as excinfo:
            conn.execute(
                original,
                {
                    "role_id": "admin",
                    "name": "Administrator",
                    "desc": "Full system access",
                    "is_sys": True,
                },
            )

    assert 'syntax error at or near ":"' in str(excinfo.value)
