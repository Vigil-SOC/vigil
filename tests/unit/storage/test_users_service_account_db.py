"""``users.service_account`` in a real Postgres: create_all, the init SQL, the migration.

A fresh install gets the column from create_all; a database built before it gets
it from ``40_users_service_account.sql`` (Compose db-seed, Helm db-init) or from
``scripts/migrate_schema.py``. Every existing row must read False: only the enable
flow makes a service account. Each change runs in a transaction that is rolled
back, so the shared test database is left as found.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from core.storage.connection import get_db_manager

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
MIGRATE_SCHEMA = REPO / "scripts" / "migrate_schema.py"
INIT_SQL = REPO / "infra" / "database" / "init" / "40_users_service_account.sql"


def _column(conn):
    return conn.execute(
        text(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'users' AND column_name = 'service_account'"
        )
    ).one_or_none()


def _step():
    # scripts/ is not a package; the migrator is a CLI file, so load it by path.
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.add_users_service_account


def _insert_user(conn, user_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO roles (role_id, name, description, permissions, "
            "is_system_role) VALUES ('role-t', 'T', 't', '{}', false) "
            "ON CONFLICT DO NOTHING"
        )
    )
    conn.execute(
        text(
            "INSERT INTO users (user_id, username, email, password_hash, "
            "full_name, role_id, is_active, is_verified, mfa_enabled, "
            "mfa_recovery_codes, login_count) "
            "VALUES (:id, :id, :email, 'x', 'x', 'role-t', true, false, false, "
            "'[]', 0)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )


def test_create_all_builds_the_column_not_null_default_false():
    with get_db_manager().engine.connect() as conn:
        data_type, nullable, default = _column(conn)
    assert data_type == "boolean"
    assert nullable == "NO"
    assert default == "false"


@pytest.mark.parametrize("apply", ["migration", "init_sql"])
def test_an_old_users_table_gains_the_column_false_for_every_row(apply):
    with get_db_manager().engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text("ALTER TABLE users DROP COLUMN service_account"))
            _insert_user(conn, "existing-user")
            assert _column(conn) is None

            for _ in range(2):  # rerunnable
                if apply == "migration":
                    _step()(conn)
                else:
                    conn.execute(text(INIT_SQL.read_text(encoding="utf-8")))

            assert _column(conn) is not None
            flag = conn.execute(
                text(
                    "SELECT service_account FROM users WHERE user_id = 'existing-user'"
                )
            ).scalar_one()
            assert flag is False
        finally:
            tx.rollback()
