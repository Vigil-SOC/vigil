"""``medic.read`` / ``medic.admin`` in a real Postgres: the init SQL and the migration.

The default roles come from ``06_auth_tables.sql`` with ``ON CONFLICT DO
NOTHING``, so a new permission there would never reach an existing install. Like
``loglm.view`` (``17_loglm_setup.sql``), the Medic grants are a later file,
``42_medic_permissions.sql`` (Compose db-seed, Helm db-init), plus the same
statement as a ``scripts/migrate_schema.py`` step. Compose's db-seed re-runs
every file on each ``up``, so a grant an operator has turned off must stay off.
Each change runs in a transaction that is rolled back.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth.auth_service import AuthService
from core.auth.permissions import MEDIC_ADMIN_PERMISSION, MEDIC_READ_PERMISSION
from core.storage.connection import get_db_manager
from tests.security.test_medic_route_matrix import MEDIC_GRANTS

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REPO = Path(__file__).resolve().parents[3]
MIGRATE_SCHEMA = REPO / "scripts" / "migrate_schema.py"
INIT_DIRS = (
    REPO / "infra" / "database" / "init",
    REPO / "infra" / "helm" / "vigil" / "files" / "database-init",
)
INIT_SQL = INIT_DIRS[0] / "42_medic_permissions.sql"
MEDIC = (MEDIC_READ_PERMISSION, MEDIC_ADMIN_PERMISSION)


def _default_roles_sql() -> str:
    """The ``INSERT INTO roles`` of 06_auth_tables.sql, verbatim."""
    source = (INIT_DIRS[0] / "06_auth_tables.sql").read_text(encoding="utf-8")
    match = re.search(
        r"INSERT INTO roles .*?ON CONFLICT \(role_id\) DO NOTHING;", source, re.S
    )
    assert match, "06_auth_tables.sql no longer seeds the default roles"
    return match.group(0)


def _step():
    # scripts/ is not a package; the migrator is a CLI file, so load it by path.
    spec = importlib.util.spec_from_file_location(
        "vigil_migrate_schema", MIGRATE_SCHEMA
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.grant_medic_permissions


def _apply(conn, how: str) -> None:
    if how == "migration":
        _step()(conn)
    else:
        conn.execute(text(INIT_SQL.read_text(encoding="utf-8")))


def _permissions(conn) -> dict[str, dict]:
    rows = conn.execute(text("SELECT role_id, permissions FROM roles")).all()
    return {role_id: perms for role_id, perms in rows}


@pytest.fixture
def conn():
    with get_db_manager().engine.connect() as connection:
        tx = connection.begin()
        try:
            connection.execute(text("DELETE FROM roles"))
            connection.execute(text(_default_roles_sql()))
            yield connection
        finally:
            tx.rollback()


def test_the_init_file_is_the_same_in_both_init_dirs():
    texts = {(d / INIT_SQL.name).read_bytes() for d in INIT_DIRS}
    assert len(texts) == 1


@pytest.mark.parametrize("how", ["init_sql", "migration"])
def test_each_default_role_gets_its_medic_grants(conn, how):
    before = _permissions(conn)
    assert not any(k in p for p in before.values() for k in MEDIC)

    for _ in range(2):  # rerunnable
        _apply(conn, how)

    after = _permissions(conn)
    assert set(after) == set(MEDIC_GRANTS)
    for role_id, perms in after.items():
        held = {k for k in MEDIC if perms.get(k, False) is True}
        assert held == MEDIC_GRANTS[role_id], role_id
        # Nothing else changed.
        rest = {k: v for k, v in perms.items() if k not in MEDIC}
        assert rest == before[role_id], role_id


@pytest.mark.parametrize("how", ["init_sql", "migration"])
def test_a_grant_an_operator_turned_off_stays_off(conn, how):
    conn.execute(
        text(
            "UPDATE roles SET permissions = permissions || "
            "CAST(:p AS jsonb) WHERE role_id = 'role-manager'"
        ),
        {"p": json.dumps({MEDIC_READ_PERMISSION: False})},
    )
    # Admin: one key turned off, the other not set yet, so the row is updated.
    conn.execute(
        text(
            "UPDATE roles SET permissions = permissions || "
            "CAST(:p AS jsonb) WHERE role_id = 'role-admin'"
        ),
        {"p": json.dumps({MEDIC_ADMIN_PERMISSION: False})},
    )

    _apply(conn, how)

    perms = _permissions(conn)
    assert perms["role-manager"][MEDIC_READ_PERMISSION] is False
    assert perms["role-admin"][MEDIC_ADMIN_PERMISSION] is False
    assert perms["role-admin"][MEDIC_READ_PERMISSION] is True


@pytest.mark.parametrize("how", ["init_sql", "migration"])
def test_a_missing_default_role_is_not_created(conn, how):
    conn.execute(text("DELETE FROM roles WHERE role_id = 'role-manager'"))

    _apply(conn, how)

    assert "role-manager" not in _permissions(conn)


def test_the_medic_service_account_holds_neither(conn, monkeypatch):
    """V1's gateway login is a Viewer: it must not read Medic through the backend."""
    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: False)
    _apply(conn, "init_sql")
    conn.execute(
        text(
            "INSERT INTO users (user_id, username, email, password_hash, "
            "full_name, role_id, is_active, is_verified, mfa_enabled, "
            "mfa_recovery_codes, login_count, service_account) "
            "VALUES ('svc-medic', 'svc-medic', 'svc@example.test', 'x', 'x', "
            "'role-viewer', true, true, false, '[]', 0, true)"
        )
    )
    session = Session(bind=conn)
    for permission in MEDIC:
        assert AuthService.check_permission("svc-medic", permission, session) is False
    # The check reads the seeded grant: an Admin in the same database holds both.
    conn.execute(
        text("UPDATE users SET role_id = 'role-admin' WHERE user_id = 'svc-medic'")
    )
    session.expire_all()
    for permission in MEDIC:
        assert AuthService.check_permission("svc-medic", permission, session) is True
